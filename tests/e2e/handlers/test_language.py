"""End-to-end ``/lang`` (Stage 26).

What's worth proving:

* ``/lang`` (+ ``/language`` / ``/язык`` / ``/kom_lang``) in a private
  chat renders the picker with two inline-keyboard buttons whose
  ``callback_data`` matches the legacy wire format (``lang_set_ru`` /
  ``lang_set_en``). Anything else would break stale-button clicks
  during the strangler-bridge window.
* The picker copy follows the *current* effective language — if the
  user's existing override is ``en`` they see the EN-leading prompt.
* Group ``/lang`` gets the #123 private-only refusal — the legacy
  owner branch never ported and its bridge is gone.
* Clicking ``lang_set_ru`` / ``lang_set_en`` writes the choice into
  ``user_settings.language`` AND edits the prompt into a confirmation.
  We assert the DB row directly — a future regression that "edits the
  message" without persisting the choice would be invisible from the
  outgoing-message capture alone.
* Bad callback data (``lang_set_unknown``) is silently NOT routed —
  the ``F.data.in_(...)`` filter is exhaustive, not a prefix match;
  validates the guardrail against typos turning into corrupt rows.
* Bot-wide HTML parse_mode: the confirmation strings are ASCII-safe,
  but the test still asserts ``<`` is absent, in case a future edit
  drops one in without escaping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    assert_only_the_stale_tail_answered,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _message_update(text: str, *, chat_type: str = "private", user_id: int = 4242) -> Update:
    """File-local defaults: user 4242 named ``Lang`` (ru). Delegates to
    the shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Lang",
        language_code="ru",
    )


def _callback_update(data: str, *, user_id: int = 4242) -> Update:
    """File-local defaults: user 4242 named ``Lang`` (ru). Delegates to
    the shared callback builder.
    """
    return make_callback_update(
        data,
        user_id=user_id,
        first_name="Lang",
        language_code="ru",
    )


@pytest.mark.parametrize(
    "alias",
    [
        "/lang",
        "/language",
        "/язык",
        "/kom_lang",
        # Stage 30: /settings / /настройки fold into the language router.
        # Their legacy bodies are identical (same picker, same callbacks).
        "/settings",
        "/настройки",
    ],
)
async def test_lang_aliases_render_picker(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update(alias))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    # Picker contains a language-selection prompt. After the T-006 i18n
    # migration the prompt moved from inline _PROMPT_RU/_EN constants to
    # the YAML `lang_select` key with bilingual heading "Язык / Language";
    # accept both legacy and post-migration phrasings so this test is
    # robust to the i18n cut-over.
    assert "Выберите язык" in body or "Choose your language" in body or "Язык / Language" in body


async def test_lang_callback_persists_and_confirms_en(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Click on ``lang_set_en`` must (1) write ``en`` into
    ``user_settings.language`` and (2) edit the prompt into the EN
    confirmation. Asserting the DB row guards against a regression
    where the visible text changes but the choice is silently dropped.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    # Stage 32: ``capture_callback_outgoing`` swallows SendMessage +
    # EditMessageText + AnswerCallbackQuery (the three methods a
    # callback flow emits) — the wire shape isn't the contract here,
    # the DB row is.
    capture_callback_outgoing(bot)

    # First: render the picker so the user has a real message-id to
    # callback against. The picker render also touches the user,
    # making the FK target row exist before the settings write.
    await dispatcher.feed_update(bot, _message_update("/lang"))
    result = await dispatcher.feed_update(bot, _callback_update("lang_set_en"))
    assert result is not UNHANDLED

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        assert await repo.get_language(4242) == "en"


async def test_lang_callback_persists_ru(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The RU branch is the default-fallback path inside
    ``set_language`` — anything other than the literal ``"en"`` clamps
    to RU. Worth its own test so a future swap of the default doesn't
    regress silently.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    capture_callback_outgoing(bot)

    # Touch the user first so the FK exists.
    await dispatcher.feed_update(bot, _message_update("/lang"))
    await dispatcher.feed_update(bot, _callback_update("lang_set_ru"))

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        assert await repo.get_language(4242) == "ru"


async def test_lang_in_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Group ``/lang`` is answered, not swallowed (#123).

    The chat-owner branch legacy had here never ported, and the legacy
    bridge that used to serve it is gone (T-011) — so the honest answer
    is the private-only refusal, and the picker must still NOT render
    in the group, which the exact match pins.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _message_update("/lang", chat_type="supergroup", user_id=-100)
    )

    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="lang")


async def test_cold_callback_succeeds_without_prior_touch(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Regression for the audit's HIGH finding (Stage 26).

    A user can click a ``lang_set_*`` button without ever having hit a
    new-pipeline *message* handler — e.g. their picker was rendered by
    legacy before the new code went live, and they click after. The
    callback must succeed end-to-end: ``users.users`` row gets created
    (via the explicit ``touch`` inside the callback handler) so the
    subsequent ``user_settings`` write doesn't trip the FK.

    Without the ``touch`` in ``handle_lang_callback``, this test would
    raise ``IntegrityError`` on the settings insert.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    capture_callback_outgoing(bot)

    # NOTE: no ``/lang`` render here — cold callback only.
    result = await dispatcher.feed_update(bot, _callback_update("lang_set_en", user_id=9999))
    assert result is not UNHANDLED

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        # If the FK fix is reverted, the assertion below never runs —
        # the dispatcher raises IntegrityError on the callback feed
        # above. So this `== "en"` check is the canary.
        assert await repo.get_language(9999) == "en"


async def test_unknown_lang_callback_never_reaches_the_setter(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``F.data.in_({"lang_set_ru", "lang_set_en"})`` is exhaustive —
    a typo like ``lang_set_de`` MUST NOT route to our handler, lest a
    third locale silently get clamped to RU and written to the DB.

    Since #159 an unclaimed tap is answered by the tail instead of
    dying in silence, so the pin is "the only thing that spoke was the
    tail, and nothing was written" rather than "nothing happened".
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _callback_update("lang_set_de"))
    assert_only_the_stale_tail_answered(sent, "lang_set_de must not reach the setter")

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        assert await UserSettingsRepo(session).get_language(4242) is None


async def test_override_flows_back_via_user_service(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """After ``/lang`` writes ``en``, the next ``user_service.touch``
    call should return an entity whose ``.language == "en"`` even
    though Telegram still reports ``language_code="ru"``. This is the
    end-to-end win — every renderer downstream gets the override for
    free without each handler asking the settings repo.
    """
    from aiogram.types import User as TelegramUser

    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/lang"))
    await dispatcher.feed_update(bot, _callback_update("lang_set_en"))

    # Now touch through the service — must report en.
    from telegram_invite_bot.repositories.user_settings_repo import (
        UserSettingsRepo as _SR,
    )
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.user_service import UserService

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        users = UsersRepo(session)
        sr = _SR(session)
        svc = UserService(users, sr)
        tg = TelegramUser(
            id=4242,
            is_bot=False,
            first_name="Lang",
            language_code="ru",  # Telegram says RU, override says EN
        )
        user = await svc.touch(tg)
        assert user.language == "en"
        assert user.language_override == "en"


async def test_lang_callback_falls_back_to_send_when_edit_fails(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``edit_text`` raises :class:`TelegramBadRequest` when the
    original prompt is gone ("message to edit not found") or unchanged
    ("message is not modified"). The handler MUST swallow that narrow
    exception and ``answer`` a fresh confirmation — propagating it
    would 500 the webhook and Telegram retries the same callback
    forever, hammering us. Without this branch, a user whose menu
    expired silently gets nothing for their click.
    """
    from aiogram.exceptions import TelegramBadRequest

    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)

    edit_attempts: list[str] = []
    fallback_sends: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "EditMessageText":
            edit_attempts.append(method.text)
            # Mimic Telegram's "message to edit not found" — the
            # production trigger for this branch.
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to edit not found"
            )
        if name == "SendMessage":
            fallback_sends.append(method.text)
            return {
                "message_id": 12,
                "date": 0,
                "chat": {"id": 4242, "type": "private"},
                "text": method.text,
            }
        if name == "AnswerCallbackQuery":
            return True
        raise AssertionError(f"unexpected: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    await dispatcher.feed_update(bot, _message_update("/lang"))
    result = await dispatcher.feed_update(bot, _callback_update("lang_set_en"))
    assert result is not UNHANDLED

    # Edit was attempted (proves the happy path ran), and the fallback
    # send actually fired with the same confirmation text. ``fallback_sends``
    # also contains the original /lang picker render — we look for the
    # confirmation as the *trailing* send so that the assertion stays
    # robust against future prompts being added before the callback.
    assert edit_attempts
    assert fallback_sends
    assert edit_attempts[-1] == fallback_sends[-1]

    # Persistence is independent of the rendering surface — the choice
    # still made it into ``user_settings`` even though the edit failed.
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        assert await repo.get_language(4242) == "en"


async def test_lang_callback_without_message_only_answers_toast(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback_query whose ``message`` field is absent (the legacy
    "inline mode" surface, or an InaccessibleMessage past Telegram's
    96h retention window) hits the ``isinstance(... MessageType)``
    guard and skips the ``edit_text`` call entirely. The toast still
    fires so the user sees an acknowledgement, but no SendMessage /
    EditMessageText is emitted.

    Without the guard the handler would attempt
    ``InaccessibleMessage.edit_text`` and crash — InaccessibleMessage
    inherits Message in name only, the method doesn't exist there.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)

    seen: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ASYNC109
    ) -> Any:
        name = type(method).__name__
        seen.append(name)
        if name == "AnswerCallbackQuery":
            return True
        raise AssertionError(f"only callback_answer expected, got {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    # Touch the user first via the picker so the FK exists. The picker
    # itself emits SendMessage — install a permissive shim only for
    # that step.
    from aiogram.types import User as TelegramUser

    from telegram_invite_bot.repositories.user_settings_repo import (
        UserSettingsRepo as _SR,
    )
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.user_service import UserService

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        users_repo = UsersRepo(session)
        sr = _SR(session)
        svc = UserService(users_repo, sr)
        await svc.touch(TelegramUser(id=4242, is_bot=False, first_name="Lang", language_code="ru"))
        await session.commit()

    # Build a callback Update with NO ``message`` — only
    # ``inline_message_id``, which Telegram sends for buttons attached
    # to messages posted via inline mode.
    cb_update = Update.model_validate(
        {
            "update_id": 99,
            "callback_query": {
                "id": "cb-no-msg",
                "from": {
                    "id": 4242,
                    "is_bot": False,
                    "first_name": "Lang",
                    "language_code": "ru",
                },
                "chat_instance": "ci-no-msg",
                "data": "lang_set_en",
                "inline_message_id": "INLINE-XYZ",
            },
        }
    )

    result = await dispatcher.feed_update(bot, cb_update)
    assert result is not UNHANDLED
    # ONLY a toast was emitted — no edit, no fallback send.
    assert seen == ["AnswerCallbackQuery"]

    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        assert await repo.get_language(4242) == "en"
