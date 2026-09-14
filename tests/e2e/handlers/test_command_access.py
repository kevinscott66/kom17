"""End-to-end tests for the R4 command-access middleware (/cmdcfg half).

:class:`~telegram_invite_bot.handlers.command_access
.CommandAccessMiddleware` is attached to the root router by the
ORCHESTRATOR (main_router.py is shared), so these tests wire it onto a
bare dispatcher with a tiny probe router instead of ``make_wired`` —
the contract under test is the middleware's own decision sequence, not
the production router tree:

* min-rank 0 (catalog default) commands pass for everyone;
* below-min-rank callers are refused with the localized denial and the
  handler never runs (the update is consumed);
* callers at/above the min rank pass;
* a DB override raises a catalog-0 command's bar (and ``/cmdcfg``-set
  ranks take effect immediately — the write clears the 300s cache);
* min-rank 6 = DISABLED for everyone but developers;
* developer bypass (even over a disabled command);
* live-TG-admin bypass in groups; NO such bypass in private chats;
* anonymous-admin actors pass the RANK half (handler-level policy
  decides) but are still refused a kill-switched command, which has no
  handler-side twin (#406);
* a command written with LEADING WHITESPACE is gated like any other —
  aiogram parses with ``split(maxsplit=1)``, which drops it, so " /warn"
  routes to the handler and a raw ``startswith("/")`` here did not see a
  command at all (#404);
* infrastructure failure (override read blowing up) fails OPEN —
  a DB hiccup must not brick every command (DESIGN_RANKS.md §3) —
  EXCEPT for min-rank 6, which the middleware still refuses from the
  last override map it read successfully, because that setting has no
  handler-side twin to fall back on;
* a command carried in a MEDIA CAPTION is gated exactly like a text one
  (aiogram's ``Command`` filter matches ``message.caption``, so reading
  only ``message.text`` here was a straight bypass of the whole gate);
* an INLINE MENU TAP that reaches a catalog command is gated the same
  way (#1428) — including the paged help card, whose page number rides
  inside the action — while ``home`` and every other router's payload
  pass untouched;
* non-command chatter is untouched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.core.ranks import rank_name
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase

# Importing the rank tables registers them on ModerationBase.metadata.
from telegram_invite_bot.db.models.rank_tables import (  # noqa: F401
    CommandRankOverride,
    RankPermissionOverride,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.command_access import CommandAccessMiddleware
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.repositories.rank_repo import (
    RankRepo,
    clear_command_override_cache,
)
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services.rank_service import clear_rank_caches
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from pathlib import Path

    from telegram_invite_bot.db import EngineRegistry

_USER_ID = 11  # plain user, rank set per-test
_ADMIN_ID = 12  # reported as TG admin by the fake API
_DEV_ID = 990  # settings.bot developer
_CHAT_ID = -100500

_WARN_RAN = "WARN-HANDLER-RAN"
_HELP_RAN = "HELP-HANDLER-RAN"
_MENU_RAN = "MENU-HANDLER-RAN"


@pytest.fixture(autouse=True)
def _isolate_rank_caches() -> None:
    clear_rank_caches()


@pytest.fixture
async def wired(
    tmp_path: Path,
) -> AsyncIterator[tuple[Bot, Dispatcher, EngineRegistry, Settings]]:
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_DEV_ID),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )
    registry = build_registry(settings)
    for base, db in ((UsersBase, DBName.USERS), (ModerationBase, DBName.MODERATION)):
        engine = registry.engine(db)
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)

    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    # ONE instance on both observers, exactly as main_router.py wires it
    # (#1428): the stale-override snapshot lives on the instance.
    gate = CommandAccessMiddleware(registry, settings)
    dispatcher.message.outer_middleware(gate)
    dispatcher.callback_query.outer_middleware(gate)

    probe = Router(name="probe")

    @probe.message(Command("warn"))
    async def _warn(message: Message) -> None:
        await message.answer(_WARN_RAN)

    @probe.message(Command("help"))
    async def _help(message: Message) -> None:
        await message.answer(_HELP_RAN)

    @probe.callback_query()
    async def _tap(callback: CallbackQuery) -> None:
        await callback.answer(_MENU_RAN)

    dispatcher.include_router(probe)
    try:
        yield bot, dispatcher, registry, settings
    finally:
        await bot.session.close()
        await registry.dispose()


def _attach_fake_api(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[str],
    *,
    admin_user_ids: set[int] | None = None,
) -> None:
    """Answer ``SendMessage`` (record text) and ``GetChatMember``."""
    if admin_user_ids is None:
        admin_user_ids = {_ADMIN_ID}

    from datetime import datetime

    from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberMember, User

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendMessage":
            sink.append(method.text)
            return Message(
                message_id=100,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        if name == "GetChatMember":
            user = User(id=method.user_id, is_bot=False, first_name="U")
            if method.user_id in admin_user_ids:
                return ChatMemberAdministrator(
                    user=user,
                    can_be_edited=False,
                    is_anonymous=False,
                    can_manage_chat=True,
                    can_delete_messages=True,
                    can_manage_video_chats=True,
                    can_restrict_members=True,
                    can_promote_members=False,
                    can_change_info=True,
                    can_invite_users=True,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                )
            return ChatMemberMember(user=user)
        if name == "AnswerCallbackQuery":
            # A pass records the probe's own answer; a denial records the
            # middleware's alert. Both arrive here, so the prefix keeps
            # them apart from the SendMessage texts above.
            if method.text is not None:
                sink.append(f"CB:{method.text}")
            return True
        raise AssertionError(f"unexpected Telegram call in test: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


def _group_msg(text: str, *, user_id: int = _USER_ID, update_id: int = 1) -> Any:
    return make_message_update(
        text,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        user_id=user_id,
        update_id=update_id,
    )


def _photo_caption_msg(caption: str, *, user_id: int = _USER_ID, update_id: int = 1) -> Any:
    """A group photo whose CAPTION carries the command.

    Built by hand rather than through ``make_message_update`` because
    the whole point is a message with ``text is None`` — the shape the
    ``Command`` filter still routes (aiogram ``filters/command.py``:
    ``text = message.text or message.caption``) and the shape the
    middleware used to wave through unchecked.
    """
    from aiogram.types import Update

    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1_700_000_000,
                "chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                "from": {"id": user_id, "is_bot": False, "first_name": "U"},
                "photo": [
                    {
                        "file_id": "f",
                        "file_unique_id": "u",
                        "width": 90,
                        "height": 90,
                        "file_size": 1234,
                    }
                ],
                "caption": caption,
            },
        }
    )


async def _set_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as session:
        await UsersRepo(session).set_rank(user_id, rank)
    clear_rank_caches()


async def _set_override(registry: EngineRegistry, key: str, min_rank: int) -> None:
    async with session_for(registry, DBName.MODERATION) as session:
        await RankRepo(session).set_command_override(key, min_rank)
    clear_rank_caches()


async def test_min_rank_zero_command_passes(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/help defaults to min rank 0 (catalog) — everyone passes."""
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/help"))
    assert sink == [_HELP_RAN]


async def test_below_min_rank_denied_and_consumed(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/warn defaults to min rank 2 — a rank-0 non-admin gets the
    localized denial and the handler never runs."""
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _group_msg("/warn"))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_denied", "ru", rank_name=rank_name(2, "ru"))]


async def test_rank_at_min_passes(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_rank(registry, _USER_ID, 2)

    await dispatcher.feed_update(bot, _group_msg("/warn"))
    assert sink == [_WARN_RAN]


async def test_override_raises_bar_of_catalog_zero_command(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /cmdcfg override on a catalog-0 command gates it immediately
    (write path clears the 300s cache)."""
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "help", 3)

    await dispatcher.feed_update(bot, _group_msg("/help"))
    assert _HELP_RAN not in sink
    assert sink == [t("h_cmdaccess_denied", "ru", rank_name=rank_name(3, "ru"))]

    sink.clear()
    await _set_rank(registry, _USER_ID, 3)
    await dispatcher.feed_update(bot, _group_msg("/help", update_id=2))
    assert sink == [_HELP_RAN]


async def test_disabled_command_blocked_even_for_tg_admin(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min rank 6 = disabled (legacy /cmdcfg scale): only developers
    pass; a live TG admin is still refused."""
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink)  # _ADMIN_ID is TG admin
    await _set_override(registry, "warn", 6)

    await dispatcher.feed_update(bot, _group_msg("/warn", user_id=_ADMIN_ID))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_disabled", "ru")]


async def test_developer_bypasses_disabled_command(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)

    await dispatcher.feed_update(bot, _group_msg("/warn", user_id=_DEV_ID))
    assert sink == [_WARN_RAN]


async def test_tg_admin_bypass_in_group(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live TG admin (rank 0) passes a min-rank-2 command in a group
    (legacy: Telegram admin bypasses ranks, bot.py:7555-7577)."""
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink)  # _ADMIN_ID is TG admin

    await dispatcher.feed_update(bot, _group_msg("/warn", user_id=_ADMIN_ID))
    assert sink == [_WARN_RAN]


async def test_no_tg_admin_bypass_in_private(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private chats have no admins — a rank-0 caller is refused with
    no GetChatMember probe (the fake would raise on one... it answers,
    but the middleware must not even ask: chat.type gate)."""
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendMessage":
            from datetime import datetime

            from aiogram.types import Chat, User
            from aiogram.types import Message as _Msg

            sink.append(method.text)
            return _Msg(
                message_id=100,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"private-chat denial must not call {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    update = make_message_update("/warn", user_id=_USER_ID, chat_type="private")
    await dispatcher.feed_update(bot, update)
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_denied", "ru", rank_name=rank_name(2, "ru"))]


async def test_anonymous_admin_passes_through(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sender_chat actors are NOT rank-gated here — the handler-level
    anonymous-admin policy (R-FIX-011) stays the deciding gate."""
    from aiogram.types import Update

    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                "from": {
                    "id": 1087968824,  # GroupAnonymousBot
                    "is_bot": True,
                    "first_name": "Group",
                },
                "sender_chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                "text": "/warn",
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    assert sink == [_WARN_RAN]


async def test_leading_whitespace_does_not_bypass_the_gate(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leading space still makes " /warn" a command to aiogram.

    ``Command`` parses with ``text.split(maxsplit=1)``
    (aiogram/filters/command.py:131), and ``str.split`` with no separator
    discards leading whitespace before the prefix is ever read. Testing
    the UNSTRIPPED text here therefore did not "miss an edge case" — it
    was a second one-keystroke way around the entire gate, alongside the
    caption one below. Legacy stripped first (bot.py:42782).
    """
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _group_msg(" /warn @someone"))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_denied", "ru", rank_name=rank_name(2, "ru"))]


async def test_leading_newline_does_not_bypass_the_kill_switch(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consequence that costs money, via the whitespace vector.

    ``/cmdcfg set <cmd> 6`` is how a paid command gets switched off in a
    hurry, and that setting exists ONLY in this middleware. A newline
    typed before the slash must not lift it. ``\n`` rather than a space
    because Telegram clients trim a leading space in some inputs but
    never a line break.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink)  # _ADMIN_ID is a live TG admin
    await _set_override(registry, "warn", 6)

    await dispatcher.feed_update(bot, _group_msg("\n/warn", user_id=_ADMIN_ID))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_disabled", "ru")]


async def test_leading_whitespace_before_plain_text_stays_untouched(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripping must not turn ordinary chatter into a command.

    The text deliberately OPENS with a gated command's name: a fix that
    stripped and then looked for a slash anywhere, or dropped the slash
    check while normalising, would resolve the first token to the rank-2
    ``warn`` key and answer this message with a denial.
    """
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _group_msg("  warn everyone, no slash"))
    assert sink == []


async def test_anonymous_admin_is_still_refused_a_disabled_command(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rank half defers to the handlers; the kill switch cannot.

    ``test_anonymous_admin_passes_through`` above pins the deliberate
    pass, and it is right for ranks — an anonymous actor has no per-user
    rank, and ``handlers/moderation``'s R-FIX-011 policy is the deciding
    gate. But ``min_rank == 6`` has no handler-side twin, so passing
    these actors BEFORE that check let any group admin run a switched-off
    command simply by turning anonymous mode on. Legacy refused them:
    ``bot.py:42878`` tests ``required_rank >= 6`` before any rank lookup
    happens (#406).
    """
    from aiogram.types import Update

    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)

    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                "from": {
                    "id": 1087968824,  # GroupAnonymousBot
                    "is_bot": True,
                    "first_name": "Group",
                },
                "sender_chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                "text": "/warn",
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_disabled", "ru")]


async def test_below_min_rank_denied_in_media_caption(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command typed as a photo caption is gated like any other.

    ``Command`` routes on ``message.text or message.caption``, so a
    rank-gated command sent as a caption reaches its handler. Reading
    only ``message.text`` in the middleware therefore did not "miss an
    edge case" — it handed every caller a one-keystroke way around the
    entire /cmdcfg gate.
    """
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _photo_caption_msg("/warn"))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_denied", "ru", rank_name=rank_name(2, "ru"))]


async def test_disabled_command_blocked_in_media_caption(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min rank 6 is the owner's kill switch — a caption must not lift it.

    The rank-2 case above already fails on the bypass; this one pins the
    consequence that actually costs money: /cmdcfg set <cmd> 6 is how a
    paid command (AI, /voice) gets switched off in a hurry, and a switch
    that a photo caption walks past is not a switch.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink)  # _ADMIN_ID is a live TG admin
    await _set_override(registry, "warn", 6)

    await dispatcher.feed_update(bot, _photo_caption_msg("/warn", user_id=_ADMIN_ID))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_disabled", "ru")]


async def test_caption_developer_bypass_still_applies(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the hole must not close the door on the developer.

    Without this, "gate captions too" could be satisfied by refusing
    every caption command outright, which would be a regression wearing
    a fix's clothes.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)

    await dispatcher.feed_update(bot, _photo_caption_msg("/warn", user_id=_DEV_ID))
    assert sink == [_WARN_RAN]


async def test_plain_caption_is_not_treated_as_a_command(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caption that is not a command stays out of the gate's way.

    The caption deliberately OPENS with a gated command's name: if the
    slash check were ever skipped on the caption branch, the first token
    would resolve to the rank-2 ``warn`` key and this photo would come
    back with a denial. A caption starting with a harmless word would
    pass that mutation and prove nothing.
    """
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _photo_caption_msg("warn everyone, no slash"))
    assert sink == []


async def test_db_failure_fails_open(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override-read infrastructure failure → the command passes
    through to its handler (word-filter never-brick posture)."""
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    async def _boom(self: Any) -> dict[str, int]:
        raise RuntimeError("simulated moderation.db outage")

    monkeypatch.setattr(RankRepo, "command_overrides", _boom)

    await dispatcher.feed_update(bot, _group_msg("/warn"))
    assert sink == [_WARN_RAN]


async def test_db_failure_still_refuses_kill_switched_command(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min-rank 6 survives a later override-read outage.

    The kill switch has no handler-side twin — nothing downstream knows
    the owner turned the command off — so the broad fail-OPEN would hand
    it back to everyone. Once the map has been read once, the middleware
    answers that one question from its snapshot instead.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)

    # First pass reads the map successfully and snapshots it.
    await dispatcher.feed_update(bot, _group_msg("/warn"))

    async def _boom(self: Any) -> dict[str, int]:
        raise RuntimeError("simulated moderation.db outage")

    monkeypatch.setattr(RankRepo, "command_overrides", _boom)

    await dispatcher.feed_update(bot, _group_msg("/warn", update_id=2))
    assert _WARN_RAN not in sink
    assert sink == [t("h_cmdaccess_disabled", "ru")] * 2


async def test_db_failure_fails_open_for_a_raised_but_not_disabled_override(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised-but-not-6 override still fails OPEN — by design.

    Re-deciding it needs the actor's rank, which is the DB read that
    just failed; availability wins there. Pinning it here keeps the
    snapshot from quietly growing into a full offline gate.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 3)

    await dispatcher.feed_update(bot, _group_msg("/warn"))
    assert sink == [t("h_cmdaccess_denied", "ru", rank_name=rank_name(3, "ru"))]

    async def _boom(self: Any) -> dict[str, int]:
        raise RuntimeError("simulated moderation.db outage")

    monkeypatch.setattr(RankRepo, "command_overrides", _boom)

    await dispatcher.feed_update(bot, _group_msg("/warn", update_id=2))
    assert sink[-1] == _WARN_RAN


async def test_db_failure_before_any_successful_read_fails_open(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold-start window, now narrowed to its floor (#1917): no
    snapshot in memory AND none on disk — a deployment where ``/cmdcfg``
    has never run — still fails open, even on a kill-switched command.

    The tmp ``DATABASE_DIR`` this fixture builds is empty, which is what
    makes this the on-disk-miss case rather than a repeat of the tests
    below."""
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)

    async def _boom(self: Any) -> dict[str, int]:
        raise RuntimeError("simulated moderation.db outage")

    monkeypatch.setattr(RankRepo, "command_overrides", _boom)

    await dispatcher.feed_update(bot, _group_msg("/warn"))
    assert sink == [_WARN_RAN]


async def _run_gate(
    gate: CommandAccessMiddleware, bot: Bot, text: str, *, user_id: int = _USER_ID
) -> list[Any]:
    """Drive ``gate`` directly with one group message.

    The restart tests need a SECOND middleware instance — a fresh
    process's — and the fixture's dispatcher already owns the first
    one. Calling the middleware with a stand-in handler is the smallest
    way to get that: the return value is the list of events the handler
    saw, so an empty list means the update was consumed (denied).
    """
    message = _group_msg(text, user_id=user_id).message.as_(bot)
    ran: list[Any] = []

    async def _handler(event: Any, data: dict[str, Any]) -> Any:
        ran.append(event)
        return None

    await gate(_handler, message, {"lang": "ru"})
    return ran


async def _blow_up_override_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(self: Any) -> dict[str, int]:
        raise RuntimeError("simulated moderation.db outage")

    monkeypatch.setattr(RankRepo, "command_overrides", _boom)


async def test_the_kill_switch_survives_a_restart(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1917: the snapshot is persisted, so a NEW process can enforce
    min-rank 6 without ever having read the DB successfully itself.

    This is the case the in-memory snapshot cannot cover and the one
    that matters most: a restart into an unhealthy ``moderation.db``
    starts with nothing in memory, and the kill switch is the one
    setting with no handler-side twin to fall back on. Before this, a
    crash-loop with a sick DB handed a switched-off command back to
    everyone.
    """
    bot, dispatcher, registry, settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)

    # A healthy pass in "process one" writes the snapshot.
    await dispatcher.feed_update(bot, _group_msg("/warn"))
    assert sink == [t("h_cmdaccess_disabled", "ru")]
    assert (settings.paths.database_dir / "command_access_overrides.json").exists()

    # "Process two": a brand-new instance, and the DB is gone.
    fresh = CommandAccessMiddleware(registry, settings)
    await _blow_up_override_reads(monkeypatch)

    ran = await _run_gate(fresh, bot, "/warn")

    assert ran == []  # consumed, the handler never ran
    assert sink[-1] == t("h_cmdaccess_disabled", "ru")


async def test_an_unusable_snapshot_file_fails_open_instead_of_raising(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt snapshot is ignored, and ignoring it is the whole
    contract: this code path runs inside the ``except`` block that
    exists so a DB hiccup cannot brick every command in the bot, so it
    may not introduce a way to raise from there.
    """
    bot, _dispatcher, registry, settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "warn", 6)
    path = settings.paths.database_dir / "command_access_overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    fresh = CommandAccessMiddleware(registry, settings)
    await _blow_up_override_reads(monkeypatch)

    ran = await _run_gate(fresh, bot, "/warn")

    assert len(ran) == 1  # fail-OPEN, and no exception escaped
    assert sink == []


async def test_a_snapshot_from_a_future_version_is_not_half_understood(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``version`` is refused whole rather than mined for the
    keys that happen to still parse — a gate built out of a shape we do
    not understand is worse than no gate, because it looks like one.
    """
    bot, _dispatcher, registry, settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    path = settings.paths.database_dir / "command_access_overrides.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 99, "overrides": {"warn": 6}}', encoding="utf-8")

    fresh = CommandAccessMiddleware(registry, settings)
    await _blow_up_override_reads(monkeypatch)

    ran = await _run_gate(fresh, bot, "/warn")

    assert len(ran) == 1
    assert sink == []


async def test_the_snapshot_is_written_only_when_the_map_changes(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override read happens on every command; the write must not.

    Each write ends in an ``fsync``, and this middleware sits in front
    of every message in the bot — so the equality test in ``_min_rank``
    is load-bearing, not tidiness. Pinned here because losing it costs
    a disk sync per command and nothing would fail.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    writes: list[dict[str, int]] = []

    def _record(path: Any, overrides: dict[str, int]) -> None:
        writes.append(dict(overrides))

    monkeypatch.setattr("telegram_invite_bot.handlers.command_access._write_snapshot", _record)
    await _set_override(registry, "warn", 6)

    await dispatcher.feed_update(bot, _group_msg("/warn"))
    await dispatcher.feed_update(bot, _group_msg("/warn", update_id=2))
    await dispatcher.feed_update(bot, _group_msg("/help", update_id=3))

    assert writes == [{"warn": 6}]


async def test_non_command_text_passes_untouched(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _group_msg("warn everyone, no slash"))
    # No probe handler matches plain text; nothing was sent — and in
    # particular no denial was produced for a non-command.
    assert sink == []


# ── the inline route (#1428) ──────────────────────────────────────────
#
# The private /start welcome carries a menu whose taps reach eight of
# these same commands. The middleware used to sit on ``message`` only,
# so the owner's kill switch stopped the typed command and left the
# button — and the kill switch has no handler-side twin to catch it.


def _menu_tap(action: str, *, user_id: int = _USER_ID, update_id: int = 1) -> Any:
    """A main-menu button tap, packed the way the keyboard packs it."""
    from aiogram.types import Update

    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": user_id, "is_bot": False, "first_name": "U"},
                "chat_instance": "ci",
                "data": MainMenu(action=action).pack(),
                "message": {
                    "message_id": update_id,
                    "date": 1_700_000_000,
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "menu",
                },
            },
        }
    )


async def test_a_disabled_command_is_refused_on_its_menu_button_too(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1428 — /cmdcfg set balance 6 must switch off the button as well.

    The whole point of min-rank 6: it exists ONLY in this middleware, so
    a route that skips the middleware is not a weakened gate, it is no
    gate. ``handlers/main_menu`` renders the balance card on a tap
    without ever passing through a message.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "balance", 6)

    await dispatcher.feed_update(bot, _menu_tap("balance"))
    assert _MENU_RAN not in sink
    assert sink == [f"CB:{t('h_cmdaccess_disabled', 'ru')}"]


async def test_a_paged_help_tap_is_gated_as_the_help_command(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The help card pages in place, so page 3 arrives as ``help3``.

    The page rides inside the action rather than in a second field (see
    :class:`MainMenu`), so a literal action→key map would have gated
    page 1 and waved pages 2..n through.
    """
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "help", 6)

    await dispatcher.feed_update(bot, _menu_tap("help3"))
    assert sink == [f"CB:{t('h_cmdaccess_disabled', 'ru')}"]


async def test_an_open_menu_command_still_passes(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile defaults to catalog 0 — the gate must stay invisible."""
    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await dispatcher.feed_update(bot, _menu_tap("profile"))
    assert sink == [f"CB:{_MENU_RAN}"]


async def test_the_home_tap_is_not_a_command_and_is_never_gated(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``home`` re-renders the menu itself; there is no /home to switch
    off, and an unknown key would default to rank 0 anyway — this pins
    that it is absent from the map on purpose, not by omission."""
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "home", 6)

    await dispatcher.feed_update(bot, _menu_tap("home"))
    assert sink == [f"CB:{_MENU_RAN}"]


async def test_another_routers_callback_is_untouched(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other button in the bot goes through this middleware now.

    An unrecognised payload must pass without a DB read and without a
    guess: mapping it to a command would gate the whole bot on one dict
    lookup. Disabled ``balance`` is set to prove the pass is not simply
    "nothing was disabled".
    """
    from aiogram.types import Update

    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "balance", 6)

    update = Update.model_validate(
        {
            "update_id": 7,
            "callback_query": {
                "id": "7",
                "from": {"id": _USER_ID, "is_bot": False, "first_name": "U"},
                "chat_instance": "ci",
                "data": "shop:buy:42",
                "message": {
                    "message_id": 7,
                    "date": 1_700_000_000,
                    "chat": {"id": _USER_ID, "type": "private"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "shop",
                },
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    assert sink == [f"CB:{_MENU_RAN}"]


async def test_the_developer_passes_a_disabled_menu_button(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same precedence head as the message path (bot.py:42874)."""
    bot, dispatcher, registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_override(registry, "balance", 6)

    await dispatcher.feed_update(bot, _menu_tap("balance", user_id=_DEV_ID))
    assert sink == [f"CB:{_MENU_RAN}"]


async def test_posting_as_a_channel_you_own_does_not_clear_the_gate(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1928: a foreign ``sender_chat`` is not an anonymous admin.

    Telegram sets ``sender_chat`` in two unrelated situations, and #246
    already split them in ``handlers/moderation``: an anonymous admin of
    *this* chat (``sender_chat.id == chat.id``), and any member posting
    as a channel they own, which most supergroups permit and which
    proves only channel ownership. This gate flagged both, so one click
    in the "send as" chooser cleared every rank restriction it enforces
    — including the ones on commands whose catalog default is 0 and
    which therefore have no handler-side gate behind this one.

    The actor here is a rank-0 member of the supergroup, no live TG
    admin, posting as their own channel.
    """
    from aiogram.types import Update

    bot, dispatcher, _registry, _settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                "from": {
                    "id": 136817688,  # Channel_Bot placeholder
                    "is_bot": True,
                    "first_name": "Channel",
                },
                "sender_chat": {"id": -100999, "type": "channel", "title": "Mine"},
                "text": "/warn",
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    assert _WARN_RAN not in sink
    # #1931: the refusal names the actual blocker. The rank copy
    # would be a lie here — the person behind the channel may well
    # hold the rank; what stops them is the "send as" chooser.
    assert sink == [t("h_cmdaccess_channel_actor", "ru")]


async def test_a_slow_read_cannot_revive_the_pre_killswitch_snapshot(
    wired: tuple[Bot, Dispatcher, EngineRegistry, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1977: the #1942 window, one layer up.

    ``_min_rank`` reads the override map and then stores it on the
    instance — and on disk — as the snapshot ``_stale_denial`` falls
    back to. Between the read and the store there are awaits, so two
    updates in flight resolve last-writer-wins, and the last writer can
    be the one that read BEFORE ``/cmdcfg`` committed. The kill switch
    is then off in the only place that survives a dead ``moderation.db``.

    ``repositories/rank_repo.py`` already closed exactly this shape for
    the module-level cache with ``CacheGeneration`` (#1942). This
    middleware keeps a second copy of the same map and did not.
    """
    bot, _dispatcher, registry, settings = wired
    sink: list[str] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    gate = CommandAccessMiddleware(registry, settings)

    before_killswitch = {"warn": 2}
    after_killswitch = {"warn": 6}
    started = asyncio.Event()
    release = asyncio.Event()
    reads = {"n": 0}

    async def _reads(_self: Any) -> dict[str, int]:
        reads["n"] += 1
        if reads["n"] == 1:
            started.set()
            await release.wait()
            return dict(before_killswitch)
        return dict(after_killswitch)

    monkeypatch.setattr(RankRepo, "command_overrides", _reads)

    # Update A is inside the read when the owner disables the command.
    slow = asyncio.create_task(gate._min_rank("warn"))
    await asyncio.wait_for(started.wait(), timeout=5)
    clear_command_override_cache()

    # Update B reads the map /cmdcfg just wrote and snapshots it.
    assert await gate._min_rank("warn") == 6

    # A finishes. Its own answer may stand — it is as fresh as the read
    # that produced it — but it must not speak for the snapshot.
    release.set()
    assert await asyncio.wait_for(slow, timeout=5) == 2

    snapshot = json.loads(
        (settings.paths.database_dir / "command_access_overrides.json").read_text("utf-8")
    )
    assert snapshot["overrides"] == after_killswitch, (
        "the on-disk snapshot is what a restart into a sick DB enforces"
    )

    # And the observable consequence: with moderation.db gone, the
    # kill switch must still hold.
    await _blow_up_override_reads(monkeypatch)
    assert await _run_gate(gate, bot, "/warn") == [], (
        "a disabled command was handed back because a stale read won the race"
    )
    assert sink[-1] == t("h_cmdaccess_disabled", "ru")
