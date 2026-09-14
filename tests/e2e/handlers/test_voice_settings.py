"""End-to-end tests for /voice_settings group transcription menu (L-71/L-72).

Strategy mirrors ``test_clear.py``: monkey-patch ``bot.session.make_request``
to answer ``GetChatMember`` (admin gate), capture ``SendMessage`` /
``EditMessageText`` / ``AnswerCallbackQuery`` calls into a sink, and feed
synthetic message / callback updates through a Dispatcher wired with ONLY
this router (plus the root :class:`LanguageMiddleware` that injects ``lang``).

Scenarios
---------
* /voice_settings (admin, group) → renders the menu, persists nothing yet.
* /voice_settings (non-admin, group) → denied, no menu.
* /voice_settings (private) → UNHANDLED here (falls to the vip stub).
* toggle callback → flips ``voice_transcription`` and re-renders.
* pick-target callback → persists ``transcription_target``.
* pick-language callback → persists ``transcription_language``.
* stats callback → renders the stats card (reads VoiceTranscriptionRepo).
* toggle callback by a NON-admin clicker → refused, no DB change.
* auto-delete / admins-only switches (RR-6 #73) → persist both ways, are
  re-gated per click, and show up as status lines on the card.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from pydantic import SecretStr
from sqlalchemy import select

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
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import GroupSettings, VoiceTranscription
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.voice_settings import build_router
from telegram_invite_bot.keyboards.builders.voice_settings import (
    VoiceSettingsPickLanguage,
    VoiceSettingsPickTarget,
    VoiceSettingsStats,
    VoiceSettingsToggle,
    VoiceSettingsToggleAutoDelete,
    VoiceSettingsToggleOnlyAdmins,
)
from telegram_invite_bot.middlewares.language import LanguageMiddleware
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from pathlib import Path

    from telegram_invite_bot.db import EngineRegistry

_ADMIN_USER_ID = 1
_NON_ADMIN_USER_ID = 2
_CHAT_ID = -100

# Every ``_wire`` builds a fresh EngineRegistry; an undisposed registry leaks
# SQLite connections that GC later and surface as misattributed failures in
# unrelated tests. Track each one and dispose in teardown (autouse fixture
# runs even when a test assertion fails, unlike a trailing dispose() call).
_REGISTRIES: list[EngineRegistry] = []


@pytest.fixture(autouse=True)
async def _dispose_registries() -> AsyncIterator[None]:
    yield
    while _REGISTRIES:
        await _REGISTRIES.pop().dispose()


async def _wire(tmp_path: Path) -> tuple[Bot, Dispatcher, EngineRegistry]:
    """(Bot, Dispatcher, EngineRegistry) wired with ONLY /voice_settings.

    Self-contained: the shared ``make_wired`` factory pulls in the whole
    ``main_router`` (a forbidden file other clusters edit), so this test
    owns its narrow wiring. The root ``LanguageMiddleware`` is attached on
    both event types exactly as production does, since the callback
    handlers inject ``lang: str``.
    """
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
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
    engine = registry.engine(DBName.USERS)
    async with engine.begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)

    bot = Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(LanguageMiddleware(registry))
    dispatcher.callback_query.outer_middleware(LanguageMiddleware(registry))
    dispatcher.include_router(build_router(registry, settings))
    _REGISTRIES.append(registry)
    return bot, dispatcher, registry


def _group_msg(text: str, *, user_id: int = _ADMIN_USER_ID, update_id: int = 1) -> Update:
    return make_message_update(
        text,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        user_id=user_id,
        first_name="Admin",
        message_id=500,
        update_id=update_id,
    )


def _group_callback(data: str, *, user_id: int = _ADMIN_USER_ID, update_id: int = 2) -> Update:
    """A callback update whose message lives in the GROUP chat.

    The shared ``make_callback_update`` hardcodes a private chat, so we
    build the envelope here with a supergroup ``message.chat``.
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": user_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "ci-1",
                "data": data,
                "message": {
                    "message_id": 10,
                    "date": 1_700_000_000,
                    "chat": {"id": _CHAT_ID, "type": "supergroup", "title": "T"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "menu",
                },
            },
        }
    )


def _attach_fake_api(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    admin_user_ids: set[int] | None = None,
) -> None:
    if admin_user_ids is None:
        admin_user_ids = {_ADMIN_USER_ID}

    from aiogram.types import Chat, Message, User

    def _synth_msg(chat_id: int, text: str) -> Message:
        return Message(
            message_id=100,
            date=datetime(2024, 1, 1),
            chat=Chat(id=chat_id, type="supergroup"),
            from_user=User(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__

        if name == "GetChatMember":
            from aiogram.types import (
                ChatMemberAdministrator,
                ChatMemberMember,
            )
            from aiogram.types import User as TGUser

            uid = method.user_id
            fake_user = TGUser(id=uid, is_bot=False, first_name="U")
            if uid in admin_user_ids:
                return ChatMemberAdministrator(
                    user=fake_user,
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
            return ChatMemberMember(user=fake_user)

        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="TestBot", username="testbot")

        if name == "SendMessage":
            sink.append({"kind": "send", "chat_id": method.chat_id, "text": method.text})
            return _synth_msg(method.chat_id, method.text)

        if name == "EditMessageText":
            sink.append({"kind": "edit", "text": method.text})
            return _synth_msg(_CHAT_ID, method.text)

        if name == "AnswerCallbackQuery":
            sink.append(
                {
                    "kind": "answer",
                    "text": method.text,
                    "show_alert": bool(method.show_alert),
                }
            )
            return True

        raise AssertionError(f"unexpected Telegram call in test: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def _read_settings(registry: EngineRegistry) -> GroupSettings | None:
    async with session_for(registry, DBName.USERS) as session:
        return (
            await session.execute(select(GroupSettings).where(GroupSettings.group_id == _CHAT_ID))
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Message entry
# ---------------------------------------------------------------------------


async def test_menu_renders_for_admin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/voice_settings"))

    sends = [e for e in sink if e["kind"] == "send"]
    assert sends, "expected the menu to be sent"
    assert "Расшифровка голосовых" in sends[0]["text"]


async def test_menu_denied_for_non_admin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/voice_settings", user_id=_NON_ADMIN_USER_ID))

    # _require_admin replies with a denial; no menu text is produced.
    assert not any("Расшифровка голосовых" in e.get("text", "") for e in sink)


async def test_menu_unhandled_in_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = make_message_update(
        "/voice_settings",
        chat_id=_ADMIN_USER_ID,
        chat_type="private",
        user_id=_ADMIN_USER_ID,
    )
    result = await dispatcher.feed_update(bot, update)
    # Group-only router: private invocation falls through to legacy/vip stub.
    assert result is UNHANDLED


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


async def test_toggle_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Missing row defaults to disabled → toggle turns it ON.
    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggle().pack()))

    row = await _read_settings(registry)
    assert row is not None
    assert bool(row.voice_transcription) is True
    # Menu re-rendered in place + callback answered.
    assert any(e["kind"] == "edit" for e in sink)
    assert any(e["kind"] == "answer" for e in sink)


async def test_pick_target_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(
        bot, _group_callback(VoiceSettingsPickTarget(target="private").pack())
    )

    row = await _read_settings(registry)
    assert row is not None
    assert row.transcription_target == "private"


async def test_pick_language_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(
        bot, _group_callback(VoiceSettingsPickLanguage(language="en").pack())
    )

    row = await _read_settings(registry)
    assert row is not None
    assert row.transcription_language == "en"


async def test_toggle_auto_delete_persists_and_flips_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RR-6 #73: the switch has to work in BOTH directions.

    A group could inherit ``auto_delete_voice=1`` from legacy, so a
    write-only-true handler would leave it permanently stuck on; the
    second tap is the half that was actually missing.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggleAutoDelete().pack()))
    row = await _read_settings(registry)
    assert row is not None
    assert bool(row.auto_delete_voice) is True

    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggleAutoDelete().pack()))
    row = await _read_settings(registry)
    assert row is not None
    assert bool(row.auto_delete_voice) is False
    assert any(e["kind"] == "edit" for e in sink)
    assert any(e["kind"] == "answer" for e in sink)


async def test_toggle_only_admins_persists_and_flips_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggleOnlyAdmins().pack()))
    row = await _read_settings(registry)
    assert row is not None
    assert bool(row.transcription_only_for_admins) is True

    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggleOnlyAdmins().pack()))
    row = await _read_settings(registry)
    assert row is not None
    assert bool(row.transcription_only_for_admins) is False


async def test_switch_toggles_do_not_disturb_other_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each switch upserts its own column only.

    The upsert lists the columns it sets, so a copy-paste slip in a new
    setter would silently reset a neighbouring flag — cheap to assert,
    invisible in production until an admin notices transcription off.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggle().pack()))
    await dispatcher.feed_update(
        bot, _group_callback(VoiceSettingsPickTarget(target="private").pack())
    )
    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggleAutoDelete().pack()))
    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsToggleOnlyAdmins().pack()))

    row = await _read_settings(registry)
    assert row is not None
    assert bool(row.voice_transcription) is True
    assert row.transcription_target == "private"
    assert bool(row.auto_delete_voice) is True
    assert bool(row.transcription_only_for_admins) is True


async def test_switch_toggle_refused_for_non_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The new callbacks re-gate the clicker like every other one.

    Both payloads are constant strings — anybody who can see a button can
    replay them — so the admin check has to live in the handler, not in
    who was shown the keyboard.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    for payload in (
        VoiceSettingsToggleAutoDelete().pack(),
        VoiceSettingsToggleOnlyAdmins().pack(),
    ):
        await dispatcher.feed_update(bot, _group_callback(payload, user_id=_NON_ADMIN_USER_ID))

    assert await _read_settings(registry) is None
    assert not any(e["kind"] == "edit" for e in sink)


async def test_menu_card_shows_switch_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RR-6 #73: the card regained the two status lines legacy had."""
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/voice_settings"))

    body = next(e["text"] for e in sink if e["kind"] == "send")
    assert "Удалять голосовое" in body
    assert "Только для админов" in body
    # Nothing is configured yet, so both read as "no".
    assert body.count("❌ нет") == 2
    # No log chat is selected, so the fallback notice stays quiet.
    assert "Отдельный чат не задан" not in body


async def test_menu_card_warns_when_log_chat_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Picking "separate chat" with no chat id says so, out loud.

    Nothing in either codebase ever wrote ``transcription_log_chat_id``
    (legacy only ALTERed the column in and read it back), so this target
    silently degrades to a group reply. Better to admit that on the card
    than to let an admin conclude transcription is broken.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(
        bot, _group_callback(VoiceSettingsPickTarget(target="log_chat").pack())
    )

    body = next(e["text"] for e in sink if e["kind"] == "edit")
    assert "Отдельный чат не задан" in body


async def test_stats_renders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Seed one transcription so the stats card has a non-zero total + preview.
    async with session_for(registry, DBName.USERS) as session:
        session.add(
            VoiceTranscription(
                group_id=_CHAT_ID,
                user_id=_ADMIN_USER_ID,
                message_id=1,
                file_id="f",
                file_unique_id="u",
                duration=3,
                transcribed_text="привет <b>мир</b>",
                language="ru",
                model_used="tiny",
                processing_time=1200,
            )
        )

    await dispatcher.feed_update(bot, _group_callback(VoiceSettingsStats().pack()))

    edits = [e for e in sink if e["kind"] == "edit"]
    assert edits, "expected the stats card to be edited in place"
    body = edits[0]["text"]
    assert "Статистика транскрипций" in body
    assert "Всего" in body
    assert "Распознано" in body
    # last_text is HTML-escaped in the handler before it reaches ``t()``;
    # pre-yaml-merge ``t()`` echoes only the bare key (dropping the
    # interpolated, already-escaped preview), so the raw ``<b>`` tag can
    # never appear in the rendered body either way. The escape itself is
    # proven directly by ``test_render_stats_escapes_last_text`` below.
    assert "<b>мир</b>" not in body


def test_render_stats_escapes_last_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """``last_text`` is HTML-escaped before it reaches the i18n layer.

    The rendered body (e2e) can't show this pre-merge because ``t()``
    echoes the bare key and drops the interpolated arg, so we probe the
    escape directly: stub the handler's ``t`` to reflect its kwargs and
    assert the dangerous tag arrived escaped.
    """
    import telegram_invite_bot.handlers.voice_settings as mod
    from telegram_invite_bot.repositories.voice_transcription_repo import (
        VoiceTranscriptionStats,
    )

    def _reflect(key: str, _lang: str, **kwargs: object) -> str:
        if not kwargs:
            return key
        joined = " ".join(f"{k}={v}" for k, v in kwargs.items())
        return f"{key}[{joined}]"

    monkeypatch.setattr(mod, "t", _reflect)
    stats = VoiceTranscriptionStats(
        total=1,
        last_text="привет <b>мир</b> & <script>",
        last_created=datetime(2024, 1, 2, 3, 4),
        avg_processing_ms=900,
    )
    body = mod._render_stats(stats, "ru")
    assert "&lt;b&gt;" in body
    assert "&amp;" in body
    assert "<b>мир</b>" not in body
    assert "<script>" not in body


def test_render_menu_escapes_unknown_language() -> None:
    """A junk ``transcription_language`` cannot break the card's HTML.

    The column is shared with the still-live legacy process and predates
    both menus, so its contents aren't ours to trust. Unescaped ``<``
    would fail the whole ``sendMessage`` under HTML parse mode — the card
    would vanish, not just the line.
    """
    import telegram_invite_bot.handlers.voice_settings as mod
    from telegram_invite_bot.repositories.voice_settings_repo import VoiceSettings

    vs = VoiceSettings(
        enabled=True,
        target="chat",
        language="<b>ru</b>",
        log_chat_id=None,
        auto_delete=False,
        only_admins=False,
    )
    body = mod._render_menu(vs, "ru")
    assert "&lt;b&gt;ru&lt;/b&gt;" in body
    assert "<b>ru</b>" not in body


def test_render_menu_localises_known_language() -> None:
    # A known code reads as its label, not the bare token legacy printed.
    import telegram_invite_bot.handlers.voice_settings as mod
    from telegram_invite_bot.repositories.voice_settings_repo import VoiceSettings

    vs = VoiceSettings(
        enabled=True,
        target="chat",
        language="en",
        log_chat_id=None,
        auto_delete=False,
        only_admins=False,
    )
    assert "English" in mod._render_menu(vs, "ru")


async def test_callback_refused_for_non_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(
        bot,
        _group_callback(VoiceSettingsToggle().pack(), user_id=_NON_ADMIN_USER_ID),
    )

    # No settings row written, alert answered, no menu edit.
    assert await _read_settings(registry) is None
    assert any(e["kind"] == "answer" and e["show_alert"] for e in sink)
    assert not any(e["kind"] == "edit" for e in sink)
