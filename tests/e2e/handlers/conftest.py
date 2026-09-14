"""Shared e2e-handler fixtures (Stage 24).

Every ``tests/e2e/handlers/test_*.py`` file used to inline the same
~30-line setup: build :class:`Settings`, materialise the right
:class:`DeclarativeBase` schemas on a :class:`EngineRegistry` rooted at
``tmp_path``, attach the relevant outer middlewares to a
:class:`Dispatcher`, and include :func:`build_main_router`. The
duplication was load-bearing (each handler legitimately needs different
schemas / middlewares), but it leaked the same boilerplate into every
new test file and made it easy to drift — e.g. one file forgetting to
``await bot.session.close()``, another forgetting ``registry.dispose()``.

The factory here keeps the variance explicit (the caller declares
which schemas and middlewares it wants) while folding the rest into a
single place. Migrating a test file is mechanical: replace the
inline fixture with ``await make_wired(schemas=[UsersBase])``.

Why a factory and not a fixture per shape: handler tests vary along
two independent axes (schemas × middlewares) — a fixture per cross-
product would explode quickly. A factory call lets the test name
exactly the slice it needs in one line.

The ``capture_outgoing`` helper at the bottom does the analogous job
for the per-file ``_capture`` functions: monkey-patches
``Bot.session.make_request`` to record outgoing ``SendMessage`` /
``SendPhoto`` / ``SendDice`` calls into a uniform ``list[dict]`` sink.
Tests can still inline custom captures for unusual outbound types
(``GetMe`` in ``test_heartbeat.py``) — the helper covers only the
common case.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AiConfig,
    AiQuotaSettings,
    AppEnv,
    BotConfig,
    EconomyConfig,
    FeatureFlags,
    HelpConfig,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    StatsConfig,
    WebhookConfig,
)
from telegram_invite_bot.core.ranks import command_entry, command_key_for
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
from telegram_invite_bot.routers.main_router import build_main_router

if TYPE_CHECKING:
    from aiogram.fsm.storage.base import BaseStorage
    from sqlalchemy.orm import DeclarativeBase

    from telegram_invite_bot.db import EngineRegistry


def make_message_update(
    text: str,
    *,
    chat_id: int | None = None,
    chat_type: str = "private",
    user_id: int = 1,
    sender_is_bot: bool = False,
    first_name: str = "T",
    last_name: str | None = None,
    username: str | None = None,
    language_code: str | None = None,
    is_premium: bool | None = None,
    update_id: int = 1,
    message_id: int = 1,
    date: int = 1_700_000_000,
    reply_to_user_id: int | None = None,
    reply_to_first_name: str = "Replied",
    reply_to_username: str | None = None,
    reply_to_is_bot: bool = False,
    as_caption: bool = False,
) -> Update:
    """Build an aiogram :class:`Update` for a message-event test.

    Defaults match the most-common case in the suite (private chat,
    chat_id mirrors user_id, neutral first_name). Per-test files
    override only the dimensions that matter for their contract —
    chat_type for group fallthroughs, user_id for FK-ordering proofs,
    language_code for i18n fallback paths. Callers should NOT touch
    the message_id / date / update_id defaults unless the test
    asserts on those fields specifically.

    ``as_caption=True`` delivers ``text`` the way Telegram delivers a
    command typed under an attached photo: ``message.text`` is absent
    and the body lives in ``message.caption`` beside a ``photo`` array.
    aiogram's ``Command`` filter reads ``text or caption``, so the
    command still routes — which is exactly why handlers that parse
    their arguments out of ``message.text`` alone silently see none.
    """
    if chat_id is None:
        chat_id = user_id
    chat: dict[str, Any] = {"id": chat_id, "type": chat_type}
    if chat_type != "private":
        chat["title"] = "T"
    from_user: dict[str, Any] = {
        # ``sender_is_bot`` exists for the anonymous-admin shape: Telegram
        # delivers those as ``GroupAnonymousBot`` (``is_bot=True``) in
        # ``from_user``, which handlers must tell apart from a human.
        "id": user_id,
        "is_bot": sender_is_bot,
        "first_name": first_name,
    }
    if last_name is not None:
        from_user["last_name"] = last_name
    if username is not None:
        from_user["username"] = username
    if language_code is not None:
        from_user["language_code"] = language_code
    if is_premium is not None:
        from_user["is_premium"] = is_premium
    message_payload: dict[str, Any] = {
        "message_id": message_id,
        "date": date,
        "chat": chat,
        "from": from_user,
    }
    if as_caption:
        message_payload["caption"] = text
        # Telegram never sends a bare caption — it comes attached to
        # media, and the smallest realistic carrier is a one-size photo.
        message_payload["photo"] = [
            {
                "file_id": "photo-1",
                "file_unique_id": "photo-1u",
                "width": 90,
                "height": 90,
            }
        ]
    else:
        message_payload["text"] = text
    if reply_to_user_id is not None:
        # Minimal reply-to envelope: same chat, distinct message_id, a
        # ``from`` user. Telegram's payload also carries ``date``/``text``
        # but the handler reads only ``reply_to_message.from_user.id`` so
        # we keep the stub lean.
        reply_from: dict[str, Any] = {
            "id": reply_to_user_id,
            "is_bot": reply_to_is_bot,
            "first_name": reply_to_first_name,
        }
        if reply_to_username is not None:
            reply_from["username"] = reply_to_username
        message_payload["reply_to_message"] = {
            "message_id": message_id - 1 if message_id > 1 else 999,
            "date": date - 1,
            "chat": chat,
            "from": reply_from,
            "text": "(replied)",
        }
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": message_payload,
        }
    )


def make_callback_update(
    data: str,
    *,
    user_id: int = 1,
    first_name: str = "T",
    language_code: str | None = None,
    update_id: int = 2,
    callback_id: str = "cb-1",
    chat_instance: str = "ci-1",
    message_id: int = 10,
    message_date: int = 1_700_000_000,
    message_text: str = "old prompt",
    chat_id: int | None = None,
    chat_type: str = "private",
    chat_title: str | None = None,
) -> Update:
    """Build an :class:`Update` for an inline-keyboard callback test.

    The synthetic ``message`` field is non-empty so
    ``callback.message.edit_text`` has fields to serialise — without
    it aiogram raises during serialisation. The defaults are fine for
    callback tests that only assert on the DB row / outgoing wire;
    tests that assert on the prompt text the user originally saw can
    override ``message_text``.

    ``chat_id`` / ``chat_type`` / ``chat_title`` place the card in a
    GROUP instead of the caller's private chat — needed by panels whose
    handlers derive the target group from ``callback.message.chat`` and
    refuse anything that is not a group (``/groupadmin``). ``chat_id``
    defaults to ``user_id``, which is the private-chat convention every
    existing caller relies on.
    """
    from_user: dict[str, Any] = {
        "id": user_id,
        "is_bot": False,
        "first_name": first_name,
    }
    if language_code is not None:
        from_user["language_code"] = language_code
    chat: dict[str, Any] = {
        "id": user_id if chat_id is None else chat_id,
        "type": chat_type,
    }
    if chat_title is not None:
        chat["title"] = chat_title
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "from": from_user,
                "chat_instance": chat_instance,
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": message_date,
                    "chat": chat,
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": message_text,
                },
            },
        }
    )


# Map of every declarative base used in tests → which engine it lives
# on. Keep this exhaustive: a typo in a test (passing the wrong base)
# is much easier to debug when ``KeyError: <BaseName>`` points right at
# it than when a foreign-key error surfaces three layers deeper.
def _base_to_db_map() -> dict[type[DeclarativeBase], DBName]:
    """Lazy import so test collection doesn't pay the cost when no
    e2e tests run.
    """
    from telegram_invite_bot.db.models.base import (
        EconomyBase,
        MessageStatsBase,
        ModerationBase,
        UsersBase,
    )

    return {
        UsersBase: DBName.USERS,
        EconomyBase: DBName.ECONOMY,
        MessageStatsBase: DBName.MESSAGE_STATS,
        ModerationBase: DBName.MODERATION,
    }


class WiredFactory(Protocol):
    """Signature of the ``make_wired`` factory.

    Spelt out as a ``Protocol`` (instead of a bare
    ``Callable[..., Awaitable[...]]``) so IDEs and mypy see the
    keyword-only parameters at call sites. The previous plain-Callable
    alias swallowed them into ``...`` and surfaced no autocomplete.
    """

    async def __call__(
        self,
        *,
        schemas: Sequence[type[DeclarativeBase]] = (),
        session_middleware: bool = False,
        throttle_middleware: bool = False,
        app_env: AppEnv = AppEnv.DEV,
        bot_config: BotConfig | None = None,
        stats_config: StatsConfig | None = None,
        help_config: HelpConfig | None = None,
        economy_config: EconomyConfig | None = None,
        ai_quota: AiQuotaSettings | None = None,
        ai_config: AiConfig | None = None,
        features: FeatureFlags | None = None,
        storage: BaseStorage | None = None,
    ) -> tuple[Bot, Dispatcher, EngineRegistry]: ...


@pytest.fixture
async def make_wired(
    tmp_path: Path,
) -> AsyncIterator[WiredFactory]:
    """Factory: build (Bot, Dispatcher, EngineRegistry) with the schemas
    and middlewares the test asks for. Single teardown closes the bot
    session and disposes the registry regardless of how many factory
    calls the test makes (in practice: one).

    Usage::

        async def test_thing(make_wired):
            bot, dispatcher, registry = await make_wired(
                schemas=[UsersBase],
                session_middleware=True,
            )

    Parameters on the factory:

    * ``schemas``: iterable of :class:`DeclarativeBase` subclasses to
      ``create_all`` on their owning engine before the test starts.
      Empty by default — handlers that don't touch DB (e.g. ``vip``,
      ``heartbeat``) skip schema creation entirely.
    * ``session_middleware``: if ``True`` (default ``False``), attaches
      :class:`SessionMiddleware` as an outer message middleware. Set
      ``True`` for handlers that depend on the request-scoped
      ``user_service`` (``start``, ``profile``, ``help``, ``support``,
      ``jokes``). Handlers with scoped middlewares (economy, stats,
      etc.) attach those internally via their ``build_router`` calls,
      so the caller doesn't need to repeat them here.
    * ``app_env``: defaults to :attr:`AppEnv.DEV`. Tests that need
      production-only safety listeners (DDL block, see ``db/safety``)
      override.
    """
    created: list[tuple[Bot, EngineRegistry]] = []

    async def _factory(
        *,
        schemas: Sequence[type[DeclarativeBase]] = (),
        session_middleware: bool = False,
        throttle_middleware: bool = False,
        app_env: AppEnv = AppEnv.DEV,
        bot_config: BotConfig | None = None,
        stats_config: StatsConfig | None = None,
        help_config: HelpConfig | None = None,
        economy_config: EconomyConfig | None = None,
        ai_quota: AiQuotaSettings | None = None,
        ai_config: AiConfig | None = None,
        features: FeatureFlags | None = None,
        storage: BaseStorage | None = None,
    ) -> tuple[Bot, Dispatcher, EngineRegistry]:
        kwargs: dict[str, Any] = {}
        if stats_config is not None:
            kwargs["stats"] = stats_config
        # Same hermeticity argument as ``features`` / ``ai`` below: an
        # unset ``help`` lets HelpConfig read the developer's real
        # ``.env``, so a box with TELEGRAPH_COMMANDS_URL configured would
        # grow an extra button in /help and /faq and fail the
        # "no URL → no button" assertions. Explicit empty by default.
        kwargs["help"] = help_config or HelpConfig(
            TELEGRAPH_COMMANDS_URL=None, TELEGRAPH_COMMANDS_URL_EN=None
        )
        if economy_config is not None:
            kwargs["economy"] = economy_config
        if ai_quota is not None:
            kwargs["ai_quota"] = ai_quota
        bot_cfg = bot_config or BotConfig(BOT_TOKEN=SecretStr("123:abc"))
        settings = Settings(
            app_env=app_env,
            bot=bot_cfg,
            webhook=WebhookConfig(),
            paths=PathsConfig(
                DATABASE_DIR=tmp_path / "db",
                MESSAGE_STATS_DIR=tmp_path / "db",
                LOGS_DIR=tmp_path / "logs",
            ),
            logging=LoggingConfig(),
            observability=ObservabilityConfig(),
            # Hermetic by default: without these two overrides the
            # sub-configs read the developer's real ``.env``, and a box
            # that has DEEPSEEK_API_KEY set would make ``/quote`` place a
            # live DeepSeek call from the test suite. ``JOKE_OFFLINE_ONLY``
            # does the same for ``/joke``'s third-party humour APIs.
            # Tests that WANT those paths patch the service seam instead.
            features=features or FeatureFlags(JOKE_OFFLINE_ONLY=True),
            ai=ai_config or AiConfig(DEEPSEEK_API_KEY=None),
            **kwargs,
        )
        registry = build_registry(settings)

        base_to_db = _base_to_db_map()
        for base in schemas:
            if base not in base_to_db:
                raise KeyError(
                    f"Unknown declarative base {base!r} — add it to "
                    "_base_to_db_map() in conftest.py"
                )
            engine = registry.engine(base_to_db[base])
            async with engine.begin() as conn:
                await conn.run_sync(base.metadata.create_all)

        # Drive the Bot from the same BotConfig as the rest of the app.
        # Hardcoding ``token="123:abc"`` here would silently shadow a
        # caller-supplied ``bot_config=BotConfig(BOT_TOKEN=...)`` — the
        # Settings would carry the override but Bot.session would not,
        # which trips up any handler that derives behaviour from
        # ``bot.token`` (deep-link builders, admin checks keyed off the
        # bot account, etc.). Stage 25 follow-up: audit caught this
        # when the override kwarg was first added.
        bot = Bot(
            token=bot_cfg.token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        # ``MemoryStorage`` by default: it is the fastest and every
        # handler test that only needs FSM state to *survive* between
        # fed updates is well served by it. A test that needs the FSM
        # calls to actually *await* — because it is about what two
        # concurrent updates see between a read and a write — passes
        # the real ``SQLiteStorage`` instead; MemoryStorage's methods
        # never suspend, so with it the interleave under test cannot
        # occur and the guard would pass vacuously. The caller owns
        # the storage it supplies, including closing it.
        dispatcher = Dispatcher(storage=storage or MemoryStorage())
        if session_middleware:
            # Mirror the prod wiring from di/providers.py: message AND
            # callback_query share the SessionMiddleware contract, so
            # ``/lang``-style flows (and any future inline-keyboard
            # handler) see the same ``user_service`` / repos as the
            # message side. Separate instance per event type matches
            # prod (see comment in di/providers.py).
            dispatcher.message.outer_middleware(SessionMiddleware(registry))
            dispatcher.callback_query.outer_middleware(SessionMiddleware(registry))
        # Throttle instance is shared with the router so the
        # ``/admin_rate_stats`` handler can read the same buckets that
        # actually gated traffic. We don't attach it as middleware in
        # tests — throttling is exercised in dedicated unit tests, and
        # leaving it inert here keeps unrelated handler tests from
        # flapping when the bucket math changes.
        throttle = ThrottlingMiddleware(settings.throttling)
        if throttle_middleware:
            # Opt-in: ``/admin_middlewares`` (and any future test that
            # asserts the rate-limit middleware actually got wired)
            # needs the real attachment. Default-off so unrelated
            # handler tests don't pay for the buckets — see the
            # docstring on ``session_middleware`` for the same
            # rationale.
            dispatcher.message.outer_middleware(throttle)
            dispatcher.callback_query.outer_middleware(throttle)
        dispatcher.include_router(
            build_main_router(
                registry,
                settings,
                throttle=throttle,
                get_dispatcher=lambda: dispatcher,
            )
        )

        created.append((bot, registry))
        return bot, dispatcher, registry

    try:
        yield _factory
    finally:
        for bot, registry in created:
            await bot.session.close()
            await registry.dispose()


# What the ``GetChatMemberCount`` stub reports. Exported so a test can
# assert on the rendered number without re-hardcoding it.
STUB_MEMBER_COUNT = 42


def _synth_message(chat_id: int, text: str, *, message_id: int = 1) -> Message:
    """Build a plausible :class:`Message` so aiogram's response parser
    accepts the captured method's return shape.

    The capture fixtures all need this same shape — a message with a
    bot ``from_user``, a private chat ``Chat`` matching ``chat_id``, and
    the text aiogram will round-trip back to the handler. Extracted so
    the four call sites (``SendMessage``/``SendPhoto`` in the narrow
    capture, ``SendMessage``/``SendPhoto``/``EditMessageText`` in the
    wide one) share one source-of-truth — without it, a future Message
    field becoming required would have to be added in four places.
    """
    return Message(
        message_id=message_id,
        date=datetime(2024, 1, 1),
        chat=Chat(id=chat_id, type="private"),
        from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
        text=text,
    )


def _try_capture_send(method: Any, sink: list[dict[str, Any]]) -> Any:
    """Record ``SendMessage`` / ``SendPhoto`` into ``sink``; return the
    synthesised response, or ``None`` if the method is neither.

    The narrow ``capture_outgoing`` raises on ``None`` (any other method
    is a regression by definition); the wider ``capture_callback_outgoing``
    falls through to the edit/answer branches. Splitting the recognise-
    plus-synthesise step from the unrecognised-fallback policy keeps
    that distinction intentional rather than accidental.
    """
    name = type(method).__name__
    if name == "SendMessage":
        # ``markup`` rides along (RR-6 #65): a refusal that offers a way
        # forward and one that dead-ends read identically in ``text``.
        sink.append(
            {
                "kind": "text",
                "chat_id": method.chat_id,
                "text": method.text,
                "markup": getattr(method, "reply_markup", None),
            }
        )
        return _synth_message(method.chat_id, method.text)
    if name == "SendPhoto":
        caption = getattr(method, "caption", None)
        sink.append({"kind": "photo", "chat_id": method.chat_id, "caption": caption})
        return _synth_message(method.chat_id, caption or "ok")
    if name == "SendDocument":
        # RR-6 #64: the Kom 📤 Export button hands the conversation back
        # as a ``.txt`` attachment. Both the filename and the bytes are
        # recorded — the transcript IS the deliverable here, so a test
        # that only saw "a document was sent" would assert nothing.
        document = getattr(method, "document", None)
        payload = getattr(document, "data", None)
        sink.append(
            {
                "kind": "document",
                "chat_id": method.chat_id,
                "caption": getattr(method, "caption", None),
                "filename": getattr(document, "filename", None),
                "content": payload.decode("utf-8") if isinstance(payload, bytes) else None,
            }
        )
        return _synth_message(method.chat_id, getattr(method, "caption", None) or "ok")
    if name == "GetMe":
        # R-FIX-004: /send checks ``bot.me().id`` to short-circuit the
        # bot-self path before issuing GetChat. Returning a stable
        # synthetic user keeps tests deterministic without faking the
        # whole aiogram bot.
        return TelegramUser(id=0, is_bot=True, first_name="bot")
    if name == "GetChat":
        # Legacy stub (unused by the post-R-FIX-004-fp /send path,
        # kept for other callers): non-bot private chat.
        return Chat(id=method.chat_id, type="private", first_name="X", username="human")
    if name == "GetChatMemberCount":
        # RR-1 #6: the /chatstats members block reads the live total.
        # A fixed synthetic count keeps the rendered card deterministic;
        # tests that need the failure path patch ``make_request``.
        # Intentionally NOT appended to ``sink`` — internal lookup.
        return STUB_MEMBER_COUNT
    if name == "GetChatAdministrators":
        # RR-4 #38: the /groupadmin staff roster reads the chat's admin
        # list once per page. An empty list is the neutral default — it
        # leaves the roster's OTHER half (bot rank-holders, membership-
        # probed through ``GetChatMember`` below) as what the panel
        # tests actually assert on, and it keeps every unrelated test
        # from having to think about admin objects. Tests that need a
        # populated admin list patch ``make_request`` directly.
        # Intentionally NOT appended to ``sink`` — internal lookup.
        return []
    if name == "GetChatMember":
        # R-FIX-004-fp: /send calls ``bot.get_chat_member`` to detect
        # bot recipients on every resolution path. Synthesise a
        # non-bot ``ChatMemberMember`` so existing tests pass
        # unchanged. Tests that need the bot path monkey-patch
        # ``bot.session.make_request`` directly. Intentionally NOT
        # appended to ``sink`` — internal lookup.
        from aiogram.types import ChatMemberMember as _CMM

        target_user = TelegramUser(id=method.user_id, is_bot=False, first_name="X")
        return _CMM(user=target_user)
    return None


def assert_chat_scope_refusal(
    sent: list[dict[str, Any]],
    *,
    scope: str,
    command: str,
    lang: str = "ru",
) -> None:
    """Assert ``sent`` holds exactly the #123 wrong-chat-type refusal.

    ``scope`` names where the command actually works — the same word
    the module passes to
    :func:`~telegram_invite_bot.handlers.chat_scope.with_chat_type_refusal`
    — so a test reads as "this is a group command, invoked in a DM".

    Before #123 these call sites asserted ``UNHANDLED`` and an empty
    sink: a command in the wrong chat type matched nothing and the user
    heard silence. The half worth keeping is the *rest* of each test —
    that the refusal path performs no side effect — so the tests were
    rewritten rather than deleted, and this helper carries the one
    assertion they now share.

    The text is compared against the rendered i18n string, not a
    substring: a refusal that lost its ``{command}`` interpolation
    would still contain the fixed half of the sentence.
    """
    key = "h_group_only_command" if scope == "group" else "h_private_only_command"
    assert [e["text"] for e in sent if e["kind"] == "text"] == [t(key, lang, command=command)]


def assert_unknown_form_hint(
    sent: list[dict[str, Any]],
    *,
    command: str,
    lang: str = "ru",
) -> None:
    """Assert ``sent`` holds exactly the #158 unknown-argument-form hint.

    Sibling of :func:`assert_chat_scope_refusal`, and it exists for the
    same reason. Those call sites asserted ``UNHANDLED`` because a
    command in the wrong *chat type* matched nothing; these asserted it
    because a command with an off-contract *argument shape* matched
    nothing. Both were correct while the telebot monolith ran beside
    us and owned the unmatched form. It is gone, so both were silence,
    and both are now an answer.

    Rebuilt from the catalog rather than hard-coded so a reworded
    ``h_cmd_<key>`` description doesn't have to be chased through every
    caller — the thing under test is that the hint names *this* command
    and carries *its* description, not the wording of either.

    The catalog row is asserted, not tolerated: the hint is registered
    only for commands ``/help`` can describe (that filter is what keeps
    the ``/admin_*`` console out of it), so a caller naming a row-less
    command is asking for an answer the bot must never give.
    """
    key = command_key_for(command)
    assert command_entry(key) is not None, f"/{command} is not a describable command"
    assert [e["text"] for e in sent if e["kind"] == "text"] == [
        t("h_unknown_form", lang, command=command, description=t(f"h_cmd_{key}", lang))
    ]


@pytest.fixture
def capture_outgoing(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Bot], list[dict[str, Any]]]:
    """Return a function that, given a :class:`Bot`, monkey-patches its
    session to record outgoing ``SendMessage`` / ``SendPhoto`` /
    ``SendDice`` calls into a fresh ``list[dict]`` sink and returns it.

    Each recorded entry is a dict with a ``kind`` key
    (``"text"`` / ``"photo"`` / ``"dice"``) plus the payload fields
    callers care about. The synthesised return value is a plausible
    :class:`Message` so aiogram's response parser doesn't complain.

    Unusual outbound types (``GetMe`` in heartbeat, callback answers)
    aren't covered here — those tests keep their bespoke capture
    closures because adding more ``kind`` branches would couple this
    helper to every handler that has odd outbound shapes.
    """

    def _attach(bot: Bot) -> list[dict[str, Any]]:
        sink: list[dict[str, Any]] = []

        async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
            response = _try_capture_send(method, sink)
            if response is None:
                raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")
            return response

        monkeypatch.setattr(bot.session, "make_request", fake_make_request)
        return sink

    return _attach


@pytest.fixture
def assert_no_outgoing(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Bot, str], None]:
    """Patch ``bot.session.make_request`` to raise on ANY outgoing call.

    The "must fall through" tests in this directory share a uniform
    shape — they feed an update that should hit UNHANDLED, and any
    outbound Telegram call is a regression by definition. Twelve+ test
    bodies used to inline a four-line ``fake_make_request`` that did
    exactly this; the fixture collapses each to a one-liner.

    Usage::

        async def test_falls_through(make_wired, assert_no_outgoing):
            bot, dispatcher, _ = await make_wired(...)
            assert_no_outgoing(bot, "group /foo should fall through")
            result = await dispatcher.feed_update(bot, ...)
            assert result is UNHANDLED

    The ``message`` argument is interpolated into the
    :class:`AssertionError` so a failing test points at the *exact*
    contract that broke, not just "unexpected call" — debugging stays
    cheap.

    NOT a drop-in for tests that need to inspect WHICH method was
    attempted (rare — heartbeat's counting fake). Those keep their
    bespoke captures.
    """

    def _attach(bot: Bot, message: str) -> None:
        async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
            raise AssertionError(f"{message} (got {type(method).__name__})")

        monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    return _attach


@pytest.fixture
def capture_callback_outgoing(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Bot], list[dict[str, Any]]]:
    """Like :func:`capture_outgoing` but additionally swallows
    ``EditMessageText`` and ``AnswerCallbackQuery`` — the two extra
    methods aiogram emits from a callback_query handler.

    Why a second fixture instead of widening ``capture_outgoing``:
    a callback test that accidentally emits ``EditMessageText`` from a
    *message* handler (no callback in flight) signals a bug, and the
    narrower helper catches it via the ``unexpected Telegram call``
    assertion. The wider helper would silently swallow that — a real
    regression we've avoided at least once during the strangler
    migration. Tests that genuinely exercise callback flows opt in to
    the looser shape explicitly.

    Recorded entries mirror the message-side fixture: ``kind`` is
    ``"text"`` / ``"photo"`` / ``"edit"`` / ``"edit_caption"`` /
    ``"callback_answer"`` so
    assertions can distinguish surfaces without inspecting raw method
    objects. ``"edit"`` entries also carry ``markup`` — the keyboard is
    half of what a re-rendered card says.
    """

    def _attach(bot: Bot) -> list[dict[str, Any]]:
        sink: list[dict[str, Any]] = []

        async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
            response = _try_capture_send(method, sink)
            if response is not None:
                return response
            name = type(method).__name__
            if name == "EditMessageText":
                # Returns a Message-shaped object to satisfy aiogram's
                # ``edit_text`` parser. The ``message_id`` is bumped to
                # 11 to distinguish a fresh post from the edited one in
                # tests that care; chat_id is taken from the method.
                # ``markup`` is recorded too (RR-4 #43): a card whose
                # buttons depend on what was read — the Words page hides
                # ➖ on an empty list — can only be pinned by looking at
                # the keyboard, not the text.
                sink.append(
                    {
                        "kind": "edit",
                        "chat_id": method.chat_id,
                        "text": method.text,
                        "markup": getattr(method, "reply_markup", None),
                    }
                )
                return _synth_message(method.chat_id, method.text, message_id=11)
            if name == "EditMessageMedia":
                # Photo-card refresh (``/profile`` preview). The caption
                # lives on the nested ``InputMediaPhoto``; surface it so
                # tests can assert the refreshed identity/balance line
                # without unpacking the raw method object.
                media = getattr(method, "media", None)
                caption = getattr(media, "caption", None) if media is not None else None
                sink.append({"kind": "edit_media", "chat_id": method.chat_id, "caption": caption})
                return _synth_message(method.chat_id, caption or "ok", message_id=11)
            if name == "EditMessageCaption":
                # The same photo, new words under it. ``/profile``'s
                # refresh lands here when the re-render failed: a photo
                # message cannot become a text one, so the stale picture
                # stays and only the numbers are replaced.
                sink.append(
                    {
                        "kind": "edit_caption",
                        "chat_id": method.chat_id,
                        "caption": getattr(method, "caption", None),
                        "markup": getattr(method, "reply_markup", None),
                    }
                )
                return _synth_message(method.chat_id, method.caption or "ok", message_id=11)
            if name == "EditMessageReplyMarkup":
                # RR-6 #64: a control tap that refreshes ONLY the keyboard
                # and leaves the answer text alone. Recorded separately
                # from ``"edit"`` so a test can tell "re-rendered the
                # buttons" from "replaced what the user was reading".
                sink.append(
                    {
                        "kind": "edit_markup",
                        "chat_id": method.chat_id,
                        "markup": getattr(method, "reply_markup", None),
                    }
                )
                return _synth_message(method.chat_id, "old prompt", message_id=11)
            if name == "AnswerCallbackQuery":
                sink.append(
                    {
                        "kind": "callback_answer",
                        "text": getattr(method, "text", None),
                        # RR-6 #66: a refusal that blocks the screen and a
                        # toast that flickers away are different products.
                        "show_alert": bool(getattr(method, "show_alert", False)),
                    }
                )
                return True
            raise AssertionError(f"unexpected Telegram call: {name}")

        monkeypatch.setattr(bot.session, "make_request", fake_make_request)
        return sink

    return _attach


def assert_only_the_stale_tail_answered(
    sink: list[dict[str, Any]], why: str, *, lang: str = "ru"
) -> None:
    """Assert nothing but the #159 tail replied to a callback tap.

    Until #159 the "this prefix is not ours" tests could say ``result
    is UNHANDLED`` and ``sent == []``: a tap no handler claimed died at
    the end of the tree without a sound. That silence WAS the bug —
    Telegram keeps the button spinning for ~15 s and then clears it
    with no explanation — so the tree now ends in an unfiltered handler
    that acknowledges whatever is left over.

    The property those tests pin has not changed and is still worth
    pinning: no feature handler may widen its filter far enough to
    claim a payload it does not own. Only its signature on the wire
    changed, from "no calls at all" to "exactly one
    ``answerCallbackQuery`` carrying the stale-card copy". An edit, a
    send, an alert, or different text all mean a real handler ran —
    which is precisely what these tests exist to catch.
    """
    assert sink == [
        {
            "kind": "callback_answer",
            "text": t("h_stale_card", lang),
            "show_alert": False,
        }
    ], why
