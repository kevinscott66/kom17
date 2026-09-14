"""End-to-end ``/groupadmin`` Words page writes (RR-4 #43).

The unit tests next door pin the keyboard and the shared validator as
pure functions. What only an end-to-end run can pin is that a tap and a
typed word reach ``word_filters`` for the RIGHT group and nowhere else —
which is the whole reason the delete button names a row id instead of
the word: Telegram caps ``callback_data`` at 64 bytes and a word may be
100 characters, so legacy's ``f"moderation_del_{word}"``
(``bot.py:32384``) is unbuildable here.

Pins:

* Page — the Words card offers ➕ always and ➖ only when there is
  something to drop.
* Drop — a tap deletes exactly one row and re-renders the grid, so an
  admin clearing several words does not re-navigate each time; the last
  word falls back to the list page.
* Drop refusals — an id belonging to ANOTHER group deletes nothing, and
  a stale id (double tap) is answered, not raised.
* Add — ➕ parks the admin in the FSM, the typed word lands in the
  tapped group's list and clears the state; an over-long or
  command-looking word writes nothing and keeps the state so the admin
  can retype instead of re-navigating.
* Scope — legacy's ``profanity_filter`` was one process-wide list, so a
  word added here was banned in every group the bot served
  (``bot.py:32413``). The other group's list must stay untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase
from telegram_invite_bot.db.models.word_filters import WordFilter
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.fsm.group_words import GroupWordsStates
from telegram_invite_bot.handlers.wordfilter import MAX_WORD_LENGTH
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.groupadmin import (
    PAGE_WORDS,
    PAGE_WORDS_DROP,
    GroupAdminRefresh,
    GroupAdminWordAdd,
    GroupAdminWordDrop,
)
from telegram_invite_bot.repositories.word_filter_repo import WordFilterRepo
from telegram_invite_bot.services.rank_service import clear_rank_caches
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from aiogram import Dispatcher

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_GROUP = -1001234
_OTHER_GROUP = -1009999
_DEV = 42
_ADMIN = 43  # no bot rank — authorised here by live TG adminship alone
_PLAIN = 46  # no rank, no TG-admin — must not reach the write buttons


@pytest.fixture(autouse=True)
def _isolate_rank_caches() -> None:
    """Ranks are cached module-level for 300s and these tests reuse small
    ids against fresh tmp DBs. Same contract as ``test_moderation.py``.
    """
    clear_rank_caches()


async def _wired(make_wired: WiredFactory) -> Any:
    return await make_wired(
        schemas=[ModerationBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )


async def _seed_words(registry: EngineRegistry, group_id: int, *words: str) -> dict[str, int]:
    """Insert ``words`` for ``group_id``; return ``{word: row id}``.

    Through the repo, not raw rows: the repo owns normalisation and the
    ``created_at`` stamp, and seeding around it would seed rows the
    production path could never produce.
    """
    engine = registry.engine(DBName.MODERATION)
    async with AsyncSession(engine) as session:
        repo = WordFilterRepo(session)
        for word in words:
            await repo.add(group_id=group_id, word=word, added_by=_DEV)
        await session.commit()
        return {e.word: e.id for e in await repo.list_entries(group_id=group_id)}


async def _words(registry: EngineRegistry, group_id: int) -> list[str]:
    engine = registry.engine(DBName.MODERATION)
    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(WordFilter.word).where(WordFilter.group_id == group_id).order_by(WordFilter.word)
        )
        return [row[0] for row in result.all()]


def _tap(data: str, *, user_id: int = _DEV, chat_id: int = _GROUP) -> Any:
    return make_callback_update(
        data, user_id=user_id, chat_id=chat_id, chat_type="supergroup", chat_title="Клуб"
    )


def _say(text: str, *, user_id: int = _DEV, update_id: int = 3) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=_GROUP,
        chat_type="supergroup",
        update_id=update_id,
    )


def _edits(sink: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in sink if e["kind"] == "edit"]


def _alerts(sink: list[dict[str, Any]]) -> list[str | None]:
    return [e.get("text") for e in sink if e["kind"] == "callback_answer"]


def _replies(sink: list[dict[str, Any]]) -> list[str]:
    return [e["text"] for e in sink if e["kind"] == "text"]


def _as_chat_admins(bot: Any, monkeypatch: pytest.MonkeyPatch, *admin_ids: int) -> set[int]:
    """Report ``admin_ids`` as live chat admins, and hand back the set.

    The staff file's twin takes its ids by value, which is enough while
    a test only ever ADDS authority. Here the set itself is returned and
    read on every probe, because the thing under test is authority going
    AWAY mid-flow: ``is_user_admin`` caches nothing, so discarding an id
    revokes adminship from the very next update onwards. Layered on top
    of the capture fixture's patch — everything else is delegated to it,
    including the ``ChatMemberMember`` it answers for non-admins.
    """
    admins = set(admin_ids)
    inner = bot.session.make_request

    async def make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "GetChatMember" and method.user_id in admins:
            from aiogram.types import ChatMemberAdministrator
            from aiogram.types import User as TGUser

            return ChatMemberAdministrator(
                user=TGUser(id=method.user_id, is_bot=False, first_name="U"),
                can_be_edited=False,
                is_anonymous=False,
                can_manage_chat=True,
                can_delete_messages=True,
                can_manage_video_chats=True,
                can_restrict_members=True,
                can_promote_members=True,
                can_change_info=True,
                can_invite_users=True,
                can_post_stories=False,
                can_edit_stories=False,
                can_delete_stories=False,
            )
        return await inner(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", make_request)
    return admins


async def _state_name(dispatcher: Dispatcher, bot: Any, *, user_id: int = _DEV) -> str | None:
    context = dispatcher.fsm.get_context(bot, chat_id=_GROUP, user_id=user_id)
    return await context.get_state()


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_words_page_offers_drop_only_when_there_is_something_to_drop(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminRefresh(section=PAGE_WORDS).pack()))
    empty_labels = [b.text for row in _edits(sink)[0]["markup"].inline_keyboard for b in row]
    assert t("h_ga_words_btn_add", "ru") in empty_labels
    assert t("h_ga_words_btn_drop", "ru") not in empty_labels

    await _seed_words(registry, _GROUP, "спам")
    sink.clear()
    await dispatcher.feed_update(bot, _tap(GroupAdminRefresh(section=PAGE_WORDS).pack()))
    labels = [b.text for row in _edits(sink)[0]["markup"].inline_keyboard for b in row]
    assert t("h_ga_words_btn_drop", "ru") in labels


@pytest.mark.asyncio
async def test_drop_grid_lists_this_groups_words_only(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed_words(registry, _GROUP, "спам")
    await _seed_words(registry, _OTHER_GROUP, "чужое")
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminRefresh(section=PAGE_WORDS_DROP).pack()))

    edits = _edits(sink)
    assert len(edits) == 1
    assert t("h_ga_words_drop_prompt", "ru") in edits[0]["text"]
    labels = [b.text for row in edits[0]["markup"].inline_keyboard for b in row]
    assert "спам" in labels
    assert "чужое" not in labels
    # Merely opening the grid wrote nothing.
    assert await _words(registry, _GROUP) == ["спам"]


# ---------------------------------------------------------------------------
# Drop writes — and refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_deletes_exactly_one_row_and_stays_on_the_grid(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    ids = await _seed_words(registry, _GROUP, "спам", "реклама")
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordDrop(word_id=ids["спам"]).pack()))

    assert await _words(registry, _GROUP) == ["реклама"]
    assert t("h_ga_words_dropped", "ru", word="спам") in _alerts(sink)
    # Re-rendered as the grid, not the list: cleaning up several words
    # should not mean re-navigating after each tap.
    labels = [b.text for row in _edits(sink)[0]["markup"].inline_keyboard for b in row]
    assert "реклама" in labels


@pytest.mark.asyncio
async def test_dropping_the_last_word_falls_back_to_the_list(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    ids = await _seed_words(registry, _GROUP, "спам")
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordDrop(word_id=ids["спам"]).pack()))

    assert await _words(registry, _GROUP) == []
    edit = _edits(sink)[0]
    assert t("h_ga_words_empty", "ru") in edit["text"]
    labels = [b.text for row in edit["markup"].inline_keyboard for b in row]
    assert t("h_ga_words_btn_drop", "ru") not in labels


@pytest.mark.asyncio
async def test_drop_refuses_another_groups_row(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    ids = await _seed_words(registry, _OTHER_GROUP, "чужое")
    sink = capture_callback_outgoing(bot)

    # A payload crafted against a row of another group: the card carries
    # an id, so the group check is the only thing between the two lists.
    await dispatcher.feed_update(bot, _tap(GroupAdminWordDrop(word_id=ids["чужое"]).pack()))

    assert await _words(registry, _OTHER_GROUP) == ["чужое"]
    assert t("h_ga_words_drop_gone", "ru") in _alerts(sink)


@pytest.mark.asyncio
async def test_drop_of_a_stale_id_is_answered_not_raised(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)

    # Double tap on a card whose row is already gone.
    await dispatcher.feed_update(bot, _tap(GroupAdminWordDrop(word_id=987654).pack()))

    assert await _words(registry, _GROUP) == []
    assert t("h_ga_words_drop_gone", "ru") in _alerts(sink)


@pytest.mark.asyncio
async def test_drop_denied_for_a_plain_member(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    ids = await _seed_words(registry, _GROUP, "спам")
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, _tap(GroupAdminWordDrop(word_id=ids["спам"]).pack(), user_id=_PLAIN)
    )

    # The panel gate runs before the write, so nothing moved.
    assert await _words(registry, _GROUP) == ["спам"]


# ---------------------------------------------------------------------------
# Add flow (➕ → typed word)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_prompt_then_typed_word_lands_in_this_group(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordAdd().pack()))
    assert t("h_ga_words_add_prompt", "ru") in _edits(sink)[0]["text"]
    assert await _state_name(dispatcher, bot) == GroupWordsStates.awaiting_word.state
    assert await _words(registry, _GROUP) == []

    await dispatcher.feed_update(bot, _say("  СпаМ  "))

    # Normalised on the way in, exactly like /filter_add.
    assert await _words(registry, _GROUP) == ["спам"]
    # Legacy's profanity_filter was process-wide: one add banned the word
    # everywhere. Ours is per-group.
    assert await _words(registry, _OTHER_GROUP) == []
    assert t("h_wf_added", "ru", word="спам") in _replies(sink)
    assert await _state_name(dispatcher, bot) is None


@pytest.mark.asyncio
async def test_add_of_a_duplicate_says_so_and_stacks_nothing(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed_words(registry, _GROUP, "спам")
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordAdd().pack()))
    await dispatcher.feed_update(bot, _say("спам"))

    assert await _words(registry, _GROUP) == ["спам"]
    assert t("h_wf_already", "ru", word="спам") in _replies(sink)
    assert await _state_name(dispatcher, bot) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["x" * (MAX_WORD_LENGTH + 1), "/ban"])
async def test_add_refuses_a_bad_word_and_keeps_the_state(
    make_wired: WiredFactory, capture_callback_outgoing: Any, bad: str
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordAdd().pack()))
    await dispatcher.feed_update(bot, _say(bad))

    assert await _words(registry, _GROUP) == []
    # Retype, don't re-navigate — same posture as the staff grant flow.
    assert await _state_name(dispatcher, bot) == GroupWordsStates.awaiting_word.state


@pytest.mark.asyncio
async def test_add_ignores_a_third_partys_message(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordAdd().pack()))
    # The FSM key is (chat, user), so an unrelated member chatting in the
    # same group while the admin is prompted must not feed the filter.
    await dispatcher.feed_update(bot, _say("привет всем", user_id=_PLAIN))

    assert await _words(registry, _GROUP) == []
    assert await _state_name(dispatcher, bot) == GroupWordsStates.awaiting_word.state


@pytest.mark.asyncio
async def test_add_state_does_not_swallow_commands(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordAdd().pack()))
    # /cancel has to still reach its own handler while the state is set —
    # the state filter excludes messages starting with "/".
    await dispatcher.feed_update(bot, _say("/cancel"))

    assert await _words(registry, _GROUP) == []
    assert await _state_name(dispatcher, bot) is None


@pytest.mark.asyncio
async def test_add_refuses_an_actor_who_lost_adminship_after_the_prompt(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-gate in ``handle_groupadmin_word_text``, isolated.

    Every other add test drives the flow as the developer, whose
    authority cannot lapse, so the gate that runs on the typed message
    has never actually been exercised — the button gate refuses first
    or nothing refuses at all. This actor is authorised by live
    Telegram adminship alone, which is exactly the authority a group
    owner can withdraw between the ➕ tap and the word.

    The refusal arrives through ``message.answer`` rather than
    ``reply`` on purpose (``WordFilterAutomodMiddleware`` is an outer
    middleware and may already have deleted the admin's message), and
    the cleared state is the half that distinguishes a guard from a
    message the router simply never delivered.
    """
    bot, dispatcher, registry = await _wired(make_wired)
    sink = capture_callback_outgoing(bot)
    admins = _as_chat_admins(bot, monkeypatch, _ADMIN)

    await dispatcher.feed_update(bot, _tap(GroupAdminWordAdd().pack(), user_id=_ADMIN))
    parked = await _state_name(dispatcher, bot, user_id=_ADMIN)
    assert parked == GroupWordsStates.awaiting_word.state

    # Demoted in Telegram while the prompt was open.
    admins.discard(_ADMIN)
    await dispatcher.feed_update(bot, _say("спам", user_id=_ADMIN))

    assert await _words(registry, _GROUP) == []
    assert t("h_mod_no_permission", "ru") in _replies(sink)
    assert await _state_name(dispatcher, bot, user_id=_ADMIN) is None
