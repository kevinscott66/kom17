"""End-to-end ``/transfer_rights`` (L-49 ownership transfer).

Pins (legacy anchors in the handler docstring, bot.py:41603-41766):

* DM-only — a group invocation is answered with the localized
  ``h_private_only_command`` card and its deep-link button, not
  ignored: ``build_router`` returns the ``with_chat_type_refusal``
  wrapper, and ``transfer_rights`` ranks below
  ``chat_scope._SILENT_FROM_RANK`` (its ``core.ranks.COMMAND_ENTRIES``
  row).
* Picker scope — only the caller's ``bot_groups`` attributions render;
  a forged :class:`TransferPick` for someone else's group answers with
  the not-found alert and sets no state.
* Target step — unknown user re-prompts (stays in state); self-transfer
  rejected; a bot target rejected (#965, legacy bot.py:41656-41658);
  ``@username`` and numeric-id both resolve via ``users.db``.
* Membership — the recipient must be in the group at the target
  step AND again at the confirm tap (#700), because they can leave
  inside the ten-minute confirmation window.
* Confirm — the guarded UPDATE re-attributes
  ``bot_groups.added_by_user_id``; the new owner gets a best-effort DM;
  the FSM is cleared (single-shot card).
* Foreign confirm tap (payload ``owner_id`` mismatch) → alert, no write.
* Cancel at the confirm step clears state and writes nothing.
* Rate limit — a second transfer of the same group within 24h is
  blocked at the pick step.

The router is wired into its own Dispatcher here (NOT ``make_wired``)
so a failure points at this handler rather than at anything else
``main_router`` happens to register alongside it. A two-line test
middleware stands in for the root LanguageMiddleware.

Assertions go through :func:`t` rather than pinning the ``h_trights_*``
prose, so a copy edit stays a copy edit and does not fail this suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ChatMemberLeft, ChatMemberMember
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_invite_bot.app import _app_timeout_rules
from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import BotGroup, User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.transfer_rights import (
    TransferStates,
    build_router,
    clear_transfer_cooldowns,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.transfer_rights import (
    TransferDecision,
    TransferPage,
    TransferPick,
    build_pick_markup,
)
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    FsmTimeoutSweeper,
)
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from aiogram.types import TelegramObject

OWNER = 42
OTHER = 99
TARGET = 777
GROUP = -1001
FOREIGN_GROUP = -1003


class _StaticLang(BaseMiddleware):
    """Stand-in for the root LanguageMiddleware: always Russian."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["lang"] = "ru"
        return await handler(event, data)


@pytest.fixture(autouse=True)
def _isolate_cooldowns() -> None:
    clear_transfer_cooldowns()


@pytest.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[Bot, Dispatcher, EngineRegistry]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)
    registry = EngineRegistry(
        engines={DBName.USERS: engine},
        sessions={DBName.USERS: async_sessionmaker(engine, expire_on_commit=False)},
    )
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    lang = _StaticLang()
    dispatcher.message.outer_middleware(lang)
    dispatcher.callback_query.outer_middleware(lang)
    dispatcher.include_router(build_router(registry))
    try:
        yield bot, dispatcher, registry
    finally:
        await bot.session.close()
        await registry.dispose()


async def _seed_groups(registry: EngineRegistry, rows: list[tuple[int, str | None, int]]) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        for chat_id, title, added_by in rows:
            session.add(BotGroup(chat_id=chat_id, chat_title=title, added_by_user_id=added_by))
        await session.commit()


async def _seed_user(
    registry: EngineRegistry, user_id: int, username: str | None, first_name: str = "U"
) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=user_id, username=username, first_name=first_name))
        await session.commit()


async def _owner_of(registry: EngineRegistry, chat_id: int) -> int:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        row = (
            await session.execute(
                select(BotGroup.added_by_user_id).where(BotGroup.chat_id == chat_id)
            )
        ).scalar_one()
    return int(row)


def _fsm(dispatcher: Dispatcher, bot: Bot, user_id: int) -> FSMContext:
    return FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id),
    )


async def _drive_to_confirm(bot: Bot, dispatcher: Dispatcher, registry: EngineRegistry) -> None:
    """Seed + walk the flow up to the confirm card for OWNER → TARGET."""
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await _seed_user(registry, TARGET, "newguy")
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    await dispatcher.feed_update(
        bot, make_message_update("@newguy", user_id=OWNER, chat_type="private")
    )


def _target_has_left(monkeypatch: pytest.MonkeyPatch, bot: Bot) -> None:
    """Make ``get_chat_member`` report TARGET as gone from the group.

    The ``GetChatMember`` branch of ``conftest._try_capture_send``
    synthesises a present ``ChatMemberMember`` for every call — which is
    exactly
    why the happy-path tests here stay green. Overriding the
    already-patched ``make_request`` is the shape that file's own note
    sanctions ("Tests that need the bot path monkey-patch
    ``bot.session.make_request`` directly"), and *wrapping* rather than
    replacing keeps every other method flowing into the capture sink.
    """
    inner = bot.session.make_request

    async def fake(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "GetChatMember" and method.user_id == TARGET:
            return ChatMemberLeft(user=TelegramUser(id=TARGET, is_bot=False, first_name="X"))
        return await inner(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", fake)


def _target_is_a_bot(monkeypatch: pytest.MonkeyPatch, bot: Bot) -> None:
    """Make ``get_chat_member`` report TARGET as a *present* bot.

    Present on purpose: the interesting case is the one the membership
    gate would wave through. Same wrapping shape as
    :func:`_target_has_left`.
    """
    inner = bot.session.make_request

    async def fake(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "GetChatMember" and method.user_id == TARGET:
            user = TelegramUser(id=TARGET, is_bot=True, first_name="Helper", username="newguy")
            return ChatMemberMember(user=user)
        return await inner(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", fake)


@pytest.mark.asyncio
async def test_empty_picker(wired: Any, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = wired
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/transfer_rights", user_id=OWNER, chat_type="private")
    )
    assert sent[0]["text"] == t("h_trights_empty", "ru")


@pytest.mark.asyncio
async def test_picker_lists_only_own_groups(wired: Any, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    await _seed_groups(
        registry,
        [(GROUP, "Alpha", OWNER), (-1002, "Beta", OWNER), (FOREIGN_GROUP, "Gamma", OTHER)],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/transfer_rights", user_id=OWNER, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Alpha" in text
    assert "-1002" in text
    # Other users' attributions must never leak into the picker.
    assert "Gamma" not in text
    assert str(FOREIGN_GROUP) not in text


@pytest.mark.asyncio
async def test_group_invocation_is_refused(wired: Any, capture_outgoing: Any) -> None:
    """A group ``/transfer_rights`` is answered, not ignored (#123).

    Handing a group over is a DM flow — it lists the owner's groups and
    then asks for a confirmation — so the group side gets the refusal
    and the deep link instead of silence.
    """
    bot, dispatcher, _ = wired
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/transfer_rights", user_id=OWNER, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="transfer_rights")


@pytest.mark.asyncio
async def test_pick_foreign_group_rejected(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(FOREIGN_GROUP, "Gamma", OTHER)])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=FOREIGN_GROUP).pack(), user_id=OWNER)
    )
    assert [e["kind"] for e in sent] == ["callback_answer"]
    assert sent[0]["text"] == t("h_trights_not_found", "ru")
    assert await _fsm(dispatcher, bot, OWNER).get_state() is None


@pytest.mark.asyncio
async def test_pick_own_group_asks_for_target(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    assert edits[0]["text"] == t("h_trights_ask_target", "ru", title="Alpha", chat_id=GROUP)
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_target.state


@pytest.mark.asyncio
async def test_unknown_target_reprompts(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    await dispatcher.feed_update(
        bot, make_message_update("@ghost", user_id=OWNER, chat_type="private")
    )
    texts = [e["text"] for e in sent if e["kind"] == "text"]
    assert t("h_trights_bad_target", "ru") in texts
    # Still waiting — the user can retry without restarting the flow.
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_target.state


@pytest.mark.asyncio
async def test_self_transfer_rejected(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await _seed_user(registry, OWNER, "me")
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    await dispatcher.feed_update(
        bot, make_message_update(str(OWNER), user_id=OWNER, chat_type="private")
    )
    texts = [e["text"] for e in sent if e["kind"] == "text"]
    assert t("h_trights_self", "ru") in texts
    assert await _owner_of(registry, GROUP) == OWNER


@pytest.mark.asyncio
async def test_full_transfer_flow(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    sent = capture_callback_outgoing(bot)
    await _drive_to_confirm(bot, dispatcher, registry)
    # Confirm card went out as a reply with the ✅/❌ markup.
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_confirm.state

    await dispatcher.feed_update(
        bot,
        make_callback_update(TransferDecision(owner_id=OWNER, ok=True).pack(), user_id=OWNER),
    )
    # The attribution moved — /mygroups would now show the group to TARGET.
    assert await _owner_of(registry, GROUP) == TARGET
    assert await state.get_state() is None
    edits = [e for e in sent if e["kind"] == "edit"]
    assert any(
        e["text"]
        == t("h_trights_done", "ru", title="Alpha", target=f"@newguy (<code>{TARGET}</code>)")
        for e in edits
    )
    # Best-effort DM to the new owner landed in their private chat.
    dms = [e for e in sent if e["kind"] == "text" and e["chat_id"] == TARGET]
    assert dms
    assert dms[0]["text"] == t("h_trights_notify", "ru", title="Alpha", chat_id=GROUP)


@pytest.mark.asyncio
async def test_target_outside_the_group_is_refused(
    wired: Any, capture_callback_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#700: the recipient must already be in the group.

    Legacy gated the target step on ``is_user_in_chat``
    (bot.py:41659-41661) and this port had dropped it, so a transfer
    could hand a group to somebody who is not in it — an owner who
    cannot open what they now own, and an attribution only *they* can
    hand back.
    """
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await _seed_user(registry, TARGET, "newguy")
    sent = capture_callback_outgoing(bot)
    _target_has_left(monkeypatch, bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    await dispatcher.feed_update(
        bot, make_message_update("@newguy", user_id=OWNER, chat_type="private")
    )
    texts = [e["text"] for e in sent if e["kind"] == "text"]
    assert t("h_trights_not_in_group", "ru", title="Alpha") in texts
    # Same posture as every other target rejection: stay in the state so
    # the owner can name somebody else without restarting the picker.
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_target.state
    assert await _owner_of(registry, GROUP) == OWNER


@pytest.mark.asyncio
async def test_bot_target_is_refused(
    wired: Any, capture_callback_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#965: ownership must not land on a bot (legacy bot.py:41656-41658).

    A ``users`` row is not proof the target is human — the
    anonymous-admin sender id is a bot id like any other — and the group
    would be attributed to an account that can never open ``/mygroups``,
    with no way back except a second transfer *it* would have to make.
    The flag is read off the membership answer this step already fetches.
    """
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await _seed_user(registry, TARGET, "newguy")
    sent = capture_callback_outgoing(bot)
    _target_is_a_bot(monkeypatch, bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    await dispatcher.feed_update(
        bot, make_message_update("@newguy", user_id=OWNER, chat_type="private")
    )
    texts = [e["text"] for e in sent if e["kind"] == "text"]
    assert t("h_trights_bot_target", "ru") in texts
    # A present bot passes the membership gate, so the "not in group"
    # refusal must NOT be what answered here.
    assert t("h_trights_not_in_group", "ru", title="Alpha") not in texts
    # Same posture as every other target rejection: stay in the state.
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_target.state


@pytest.mark.asyncio
async def test_target_who_leaves_before_confirm_is_refused(
    wired: Any, capture_callback_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#700: the second check is not redundant.

    The confirm card lives ten minutes (:class:`FsmTimeoutSweeper`),
    and the target can walk out inside that window — legacy re-checked
    at exactly this point too (bot.py:41722-41724). Membership is OK at
    the target step here and gone by the tap, which is the ordering the
    single-check version would sail straight through.
    """
    bot, dispatcher, registry = wired
    sent = capture_callback_outgoing(bot)
    await _drive_to_confirm(bot, dispatcher, registry)
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_confirm.state

    _target_has_left(monkeypatch, bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(TransferDecision(owner_id=OWNER, ok=True).pack(), user_id=OWNER),
    )
    edits = [e["text"] for e in sent if e["kind"] == "edit"]
    assert t("h_trights_not_in_group", "ru", title="Alpha") in edits
    # The guarded UPDATE never ran.
    assert await _owner_of(registry, GROUP) == OWNER


@pytest.mark.asyncio
async def test_numeric_id_target_resolves(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await _seed_user(registry, TARGET, None, first_name="NoNick")
    capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    await dispatcher.feed_update(
        bot, make_message_update(str(TARGET), user_id=OWNER, chat_type="private")
    )
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_confirm.state


@pytest.mark.asyncio
async def test_foreign_confirm_tap_rejected(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    sent = capture_callback_outgoing(bot)
    await _drive_to_confirm(bot, dispatcher, registry)
    before = len(sent)
    # OTHER taps the (e.g. forwarded) card carrying OWNER's payload.
    await dispatcher.feed_update(
        bot,
        make_callback_update(TransferDecision(owner_id=OWNER, ok=True).pack(), user_id=OTHER),
    )
    # No write, no edit — just the foreign-tap alert. (OTHER has no FSM
    # state, so the StateFilter rejects before the handler; either way
    # the attribution must not move.)
    assert await _owner_of(registry, GROUP) == OWNER
    assert all(e["kind"] != "edit" for e in sent[before:])


@pytest.mark.asyncio
async def test_cancel_clears_flow(wired: Any, capture_callback_outgoing: Any) -> None:
    bot, dispatcher, registry = wired
    sent = capture_callback_outgoing(bot)
    await _drive_to_confirm(bot, dispatcher, registry)
    await dispatcher.feed_update(
        bot,
        make_callback_update(TransferDecision(owner_id=OWNER, ok=False).pack(), user_id=OWNER),
    )
    assert await _owner_of(registry, GROUP) == OWNER
    assert await _fsm(dispatcher, bot, OWNER).get_state() is None
    edits = [e for e in sent if e["kind"] == "edit"]
    assert any(e["text"] == t("h_trights_cancelled", "ru") for e in edits)


@pytest.mark.asyncio
async def test_rate_limit_blocks_second_transfer(
    wired: Any, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = wired
    sent = capture_callback_outgoing(bot)
    await _drive_to_confirm(bot, dispatcher, registry)
    await dispatcher.feed_update(
        bot,
        make_callback_update(TransferDecision(owner_id=OWNER, ok=True).pack(), user_id=OWNER),
    )
    assert await _owner_of(registry, GROUP) == TARGET
    before = len(sent)
    # The NEW owner immediately tries to transfer the same group back.
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=TARGET)
    )
    answers = [e for e in sent[before:] if e["kind"] == "callback_answer"]
    assert answers
    assert answers[-1]["text"] == t("h_trights_rate_limited", "ru")
    assert await _fsm(dispatcher, bot, TARGET).get_state() is None


def _sweeper(bot: Bot, dispatcher: Dispatcher, *, ahead: timedelta) -> FsmTimeoutSweeper:
    """Production sweeper config, clock pinned ``ahead`` of now.

    ``_app_timeout_rules()`` and not a locally-built dict: the defect
    this guards was never a missing *rule* — the rule was registered and
    correct — so a test that supplied its own would have passed against
    the broken handler.
    """
    return FsmTimeoutSweeper(
        storage=dispatcher.storage,
        bot=bot,
        rules=_app_timeout_rules(),
        clock=lambda: datetime.now(UTC) + ahead,
    )


@pytest.mark.asyncio
async def test_abandoned_target_step_expires(wired: Any, capture_callback_outgoing: Any) -> None:
    """The 10-minute rule must actually fire on an abandoned pick.

    The sweeper reads its deadline from ``state_entered_at`` in the FSM
    data, so a handler that sets a state without stamping it produces a
    session the sweeper *scans and skips forever* — the busy-gate in
    ``handle_transfer_start`` never clears and the owner is locked out
    of /transfer_rights until they think to send /cancel. Worse, the FSM
    key is (chat, user), so the stuck state also swallows /check_create
    and the P2P buy flow.
    """
    bot, dispatcher, registry = wired
    sent = capture_callback_outgoing(bot)
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    state = _fsm(dispatcher, bot, OWNER)
    assert await state.get_state() == TransferStates.awaiting_target.state
    # Parsable, not merely present: a stamp the sweeper can't read is
    # the same skip-forever branch as no stamp at all.
    stamp = (await state.get_data())[STATE_ENTERED_AT_FIELD]
    assert datetime.fromisoformat(stamp).tzinfo is not None

    fresh = await _sweeper(bot, dispatcher, ahead=timedelta(seconds=599)).sweep_once()
    assert (fresh.scanned, fresh.expired, fresh.errors) == (1, 0, 0)
    assert await state.get_state() == TransferStates.awaiting_target.state

    report = await _sweeper(bot, dispatcher, ahead=timedelta(seconds=601)).sweep_once()
    assert (report.scanned, report.expired, report.errors) == (1, 1, 0)
    assert await state.get_state() is None
    texts = [e["text"] for e in sent if e["kind"] == "text"]
    assert t("h_trights_timeout", "ru") in texts


@pytest.mark.asyncio
async def test_confirm_step_gets_its_own_budget(wired: Any, capture_callback_outgoing: Any) -> None:
    """Advancing to the confirm step re-stamps the deadline.

    Without the re-stamp the confirm card would inherit whatever was
    left of the target step's ten minutes — a user who spent nine
    minutes finding the right ``@username`` would get sixty seconds to
    read a card that hands away a group.
    """
    bot, dispatcher, registry = wired
    capture_callback_outgoing(bot)
    state = _fsm(dispatcher, bot, OWNER)
    await _seed_groups(registry, [(GROUP, "Alpha", OWNER)])
    await _seed_user(registry, TARGET, "newguy")
    await dispatcher.feed_update(
        bot, make_callback_update(TransferPick(group_id=GROUP).pack(), user_id=OWNER)
    )
    # Age the pick step to 590s — nine and a half minutes spent looking
    # up the right @username. Both stamps are otherwise written within
    # the same millisecond, so only a rewind can tell a re-stamp from a
    # carried-over one.
    await state.update_data(
        {STATE_ENTERED_AT_FIELD: (datetime.now(UTC) - timedelta(seconds=590)).isoformat()}
    )
    await dispatcher.feed_update(
        bot, make_message_update("@newguy", user_id=OWNER, chat_type="private")
    )
    assert await state.get_state() == TransferStates.awaiting_confirm.state

    # 20s later the confirm card is 20s old, not 610s: it survives.
    fresh = await _sweeper(bot, dispatcher, ahead=timedelta(seconds=20)).sweep_once()
    assert (fresh.scanned, fresh.expired) == (1, 0)
    assert await state.get_state() == TransferStates.awaiting_confirm.state

    # ...and it still expires on its own ten minutes.
    report = await _sweeper(bot, dispatcher, ahead=timedelta(seconds=601)).sweep_once()
    assert (report.scanned, report.expired) == (1, 1)
    assert await state.get_state() is None


def test_pick_markup_shape() -> None:
    """Builder contract: one pick button per group, nav row when
    paginated, cancel row always present (owner-pinned).
    """
    rows: list[tuple[int, str | None]] = [(GROUP, "Alpha"), (-1002, None)]
    markup = build_pick_markup("ru", rows=rows, page=2, total=25, owner_id=OWNER)
    flat = [btn for row in markup.inline_keyboard for btn in row]
    datas = [btn.callback_data for btn in flat]
    assert TransferPick(group_id=GROUP).pack() in datas
    assert TransferPick(group_id=-1002).pack() in datas
    # Missing title falls back to the chat id in the label.
    assert any("-1002" in (btn.text or "") for btn in flat)
    # 25 rows / 10 per page → 3 pages; page 2 has both arrows.
    assert TransferPage(page=1).pack() in datas
    assert TransferPage(page=3).pack() in datas
    assert TransferDecision(owner_id=OWNER, ok=False).pack() in datas
