"""End-to-end ``/groupadmin`` staff panel (RR-4 #38).

The unit tests next door pin the renderer and the four demote guards as
pure functions. What only an end-to-end run can pin is that those guards
are actually enforced **at write time** — that a tapped demote button
reaches ``users.rank`` exactly when it should and nowhere else, and that
a payload crafted against a target the tapper may not touch changes no
row at all.

Pins:

* Roster — the staff page lists a rank-holder who is present in the chat
  and offers the write buttons to an actor who may use them.
* Demote — a tap zeroes exactly one ``users.rank`` and re-renders.
* Demote refusals — a developer target, a rank-0 target, the actor
  themselves, and a target ranked at/above the actor all write NOTHING.
  Legacy's staff panel had none of these guards: it called
  ``set_user_rank`` straight from a group-admin callback (bot.py:31197).
* Grant — ➕ parks the admin in the FSM, the typed ``<id|@username>
  <rank>`` lands in ``users.rank`` and clears the state; a malformed
  line keeps the state (retype, don't re-navigate) and writes nothing;
  a rank outside 1..4 is refused, so the panel cannot mint an OWNER.

``GetChatAdministrators`` is stubbed empty by the shared conftest, so
every roster row here comes from the DB half — which is the half that
carries a rank worth stripping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.core.ranks import rank_name
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.fsm.group_staff import GroupStaffStates
from telegram_invite_bot.handlers.groupadmin import PANEL_GRANT_MAX
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.groupadmin import (
    PAGE_STAFF,
    PAGE_STAFF_DROP,
    GroupAdminRefresh,
    GroupAdminStaffAdd,
    GroupAdminStaffDrop,
)
from telegram_invite_bot.services.rank_service import clear_rank_caches
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from aiogram import Dispatcher

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_GROUP = -1001234
_DEV = 42
# #670: rank 4 is what lets them MANAGE ranks once inside; the panel
# itself is opened by live Telegram adminship, so every test that taps
# as _ADMIN layers `_as_chat_admins` on top.
_ADMIN = 43  # rank 4 + live TG admin — may open the panel AND manage ranks
_MOD = 44  # rank 2 — the demotable one
_PEER = 45  # rank 4 — level with _ADMIN, therefore untouchable by them
_PLAIN = 46  # rank 0 — nothing to strip
_STRANGER = 47  # rank 0, but a live Telegram admin of this chat


@pytest.fixture(autouse=True)
def _isolate_rank_caches() -> None:
    """Ranks are cached module-level for 300s and these tests reuse small
    ids against fresh tmp DBs — a stale entry would leak a rank from the
    previous test into this one. Same contract as ``test_moderation.py``.
    """
    clear_rank_caches()


async def _wired(make_wired: WiredFactory) -> Any:
    return await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )


async def _seed(
    registry: EngineRegistry,
    *people: tuple[int, int],
    username: str | None = None,
) -> None:
    """Insert ``(user_id, rank)`` rows; ``username`` lands on the first."""
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        for index, (user_id, rank) in enumerate(people):
            session.add(
                UserRow(
                    user_id=user_id,
                    rank=rank,
                    first_name=f"U{user_id}",
                    username=username if index == 0 else None,
                )
            )
        await session.commit()


async def _ranks(registry: EngineRegistry) -> dict[int, int]:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        result = await session.execute(select(UserRow.user_id, UserRow.rank))
        return {int(uid): int(rank or 0) for uid, rank in result.all()}


async def _set_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    """Rewrite an existing row's rank, then drop the 300s rank cache.

    ``_seed`` only inserts, and ``RankService`` memoises ranks for 300
    seconds — without ``clear_rank_caches`` a handler would keep reading
    the rank the actor held when the panel opened, which is precisely
    the staleness a mid-flow revocation test exists to rule out.
    """
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        await session.execute(update(UserRow).where(UserRow.user_id == user_id).values(rank=rank))
        await session.commit()
    clear_rank_caches()


async def _state_name(dispatcher: Dispatcher, bot: Any, *, user_id: int = _DEV) -> str | None:
    """The FSM state this actor is parked in, or ``None``.

    Same helper as ``test_groupadmin_words.py``: "ranks unchanged" is
    equally true when the guard refuses and when ``StateFilter`` never
    routes the message at all, so the state has to be read directly.
    """
    context = dispatcher.fsm.get_context(bot, chat_id=_GROUP, user_id=user_id)
    return await context.get_state()


def _tap(data: str, *, user_id: int = _DEV) -> Any:
    return make_callback_update(
        data, user_id=user_id, chat_id=_GROUP, chat_type="supergroup", chat_title="Клуб"
    )


def _say(text: str, *, user_id: int = _DEV) -> Any:
    return make_message_update(
        text, user_id=user_id, chat_id=_GROUP, chat_type="supergroup", update_id=3
    )


def _edits(sink: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in sink if e["kind"] == "edit"]


def _alerts(sink: list[dict[str, Any]]) -> list[str | None]:
    return [e.get("text") for e in sink if e["kind"] == "callback_answer"]


def _replies(sink: list[dict[str, Any]]) -> list[str]:
    return [e["text"] for e in sink if e["kind"] == "text"]


def _as_chat_admins(bot: Any, monkeypatch: pytest.MonkeyPatch, *admin_ids: int) -> None:
    """Report ``admin_ids`` as live chat admins to ``is_user_admin``.

    Layered ON TOP of the capture fixture's patch (everything else is
    delegated), because what these tests need is one extra fact about
    the chat, not a second Telegram stub.
    """
    inner = bot.session.make_request

    async def make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "GetChatMember" and method.user_id in admin_ids:
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


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_global_rank_alone_does_not_open_the_panel(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    """#670: rank 4 without live adminship HERE gets nothing.

    Ranks are global — anybody the owner ever promoted in one chat used
    to be able to reconfigure moderation in every chat the bot sits in,
    including chats they had never been given any standing in. Legacy's
    ``has_group_admin_rights`` (bot.py:7555-7565) had no rank path at
    all; the rank fall-through lives one level up in
    ``require_group_moderation`` (bot.py:7568-7577), which guards the
    moderation COMMANDS, not this panel.
    """
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PEER, 4))
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, _tap(GroupAdminRefresh(section=PAGE_STAFF).pack(), user_id=_PEER)
    )

    assert t("h_mod_no_permission", "ru") in _alerts(sink)
    assert _edits(sink) == []


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_page_lists_rank_holders_present_in_the_chat(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_MOD, 2), (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminRefresh(section=PAGE_STAFF).pack()))

    edits = _edits(sink)
    assert len(edits) == 1
    text = edits[0]["text"]
    assert f"<code>{_MOD}</code>" in text
    # Rank 0 holds no power anywhere — it is not staff.
    assert f"<code>{_PLAIN}</code>" not in text
    assert t("h_ga_staff_hint", "ru") in text
    # A developer may manage ranks, so the write buttons are offered.
    assert t("h_ga_staff_readonly", "ru") not in text


@pytest.mark.asyncio
async def test_drop_grid_offers_only_legal_targets(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    # _ADMIN taps; _PEER is level with them and _DEV is a developer.
    await _seed(registry, (_ADMIN, 4), (_PEER, 4), (_MOD, 2), (_DEV, 6))
    sink = capture_callback_outgoing(bot)
    _as_chat_admins(bot, monkeypatch, _ADMIN)

    await dispatcher.feed_update(
        bot, _tap(GroupAdminRefresh(section=PAGE_STAFF_DROP).pack(), user_id=_ADMIN)
    )

    edits = _edits(sink)
    assert len(edits) == 1
    assert t("h_ga_staff_drop_prompt", "ru") == edits[0]["text"]
    # Nothing was written by merely opening the grid.
    assert await _ranks(registry) == {_ADMIN: 4, _PEER: 4, _MOD: 2, _DEV: 6}


# ---------------------------------------------------------------------------
# Demote writes — and refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_zeroes_exactly_one_rank(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_MOD, 2), (_PEER, 4))
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffDrop(user_id=_MOD).pack()))

    assert await _ranks(registry) == {_MOD: 0, _PEER: 4}
    assert t("h_ga_staff_dropped", "ru") in _alerts(sink)
    assert len(_edits(sink)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "target"),
    [
        # A developer's rank is immutable (legacy bot.py:31418).
        (_ADMIN, _DEV),
        # Nothing to strip.
        (_ADMIN, _PLAIN),
        # Never yourself — a panel that can demote its operator is a
        # lockout waiting to happen.
        (_ADMIN, _ADMIN),
        # Level with the actor: a rank-4 admin must not strip a peer,
        # and by the same rule cannot reach the owner above them.
        (_ADMIN, _PEER),
    ],
)
async def test_drop_refuses_and_writes_nothing(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
    actor: int,
    target: int,
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_ADMIN, 4), (_PEER, 4), (_PLAIN, 0), (_DEV, 6))
    before = await _ranks(registry)
    sink = capture_callback_outgoing(bot)
    _as_chat_admins(bot, monkeypatch, actor)

    await dispatcher.feed_update(
        bot, _tap(GroupAdminStaffDrop(user_id=target).pack(), user_id=actor)
    )

    assert await _ranks(registry) == before
    assert t("h_ga_staff_denied", "ru") in _alerts(sink)
    assert _edits(sink) == []


# ---------------------------------------------------------------------------
# Grant flow (➕ → typed line)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_prompt_then_typed_line_writes_the_rank(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack()))
    prompt = _edits(sink)[0]["text"]
    # The legend advertises 1..4 only.
    assert f"<b>{PANEL_GRANT_MAX}</b>" in prompt
    assert await _ranks(registry) == {_PLAIN: 0}

    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 2"))

    assert await _ranks(registry) == {_PLAIN: 2}
    assert t(
        "h_ga_staff_granted",
        "ru",
        user_id=_PLAIN,
        rank=rank_name(2, "ru", in_group=True),
    ) in _replies(sink)


@pytest.mark.asyncio
async def test_grant_accepts_an_at_username(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0), username="AnyaK")
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack()))
    # Case-insensitive, like every other @-target in this codebase.
    # (Telegram usernames are ASCII-only, which is what SQLite's
    # ASCII-only ``lower()`` can actually fold.)
    await dispatcher.feed_update(bot, _say("@anyak 3"))

    assert await _ranks(registry) == {_PLAIN: 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "marker"),
    [
        ("46", "h_ga_staff_bad_format"),
        ("46 2 3", "h_ga_staff_bad_format"),
        ("46 два", "h_ga_staff_bad_format"),
        # Outside 1..4: the panel structurally cannot mint an OWNER (5)
        # or a DEVELOPER (6), whoever is tapping.
        ("46 5", "h_ga_staff_bad_rank"),
        ("46 6", "h_ga_staff_bad_rank"),
        ("46 0", "h_ga_staff_bad_rank"),
        ("46 -1", "h_ga_staff_bad_rank"),
    ],
)
async def test_grant_rejects_bad_input_without_writing(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    line: str,
    marker: str,
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack()))
    await dispatcher.feed_update(bot, _say(line))

    assert await _ranks(registry) == {_PLAIN: 0}
    assert t(marker, "ru", low=1, high=PANEL_GRANT_MAX) in _replies(sink)

    # The state survives a malformed line: the admin retypes instead of
    # re-navigating the panel.
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 1"))
    assert await _ranks(registry) == {_PLAIN: 1}


@pytest.mark.asyncio
async def test_grant_refuses_a_rank_at_or_above_the_actor(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_ADMIN, 4), (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)
    _as_chat_admins(bot, monkeypatch, _ADMIN)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack(), user_id=_ADMIN))
    # A rank-4 admin handing out rank 4 would clone their own authority.
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 4", user_id=_ADMIN))

    assert await _ranks(registry) == {_ADMIN: 4, _PLAIN: 0}
    assert t("h_ga_staff_denied", "ru") in _replies(sink)


@pytest.mark.asyncio
async def test_grant_refuses_an_actor_who_lost_their_rank_after_the_prompt(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second gate, tested where it is the only one left standing.

    Every other grant test either never enters the state or keeps its
    authority throughout, so the BUTTON gate is what refuses them and
    the re-gate in ``_staff_text`` never runs at all. Here the actor is
    genuinely authorised at the tap — ``state.set_state`` really fires —
    and the rank is stripped before the line is typed, which is the case
    the re-gate's own docstring names: "the admin could have been
    demoted between the tap and the text".

    ``h_ga_staff_denied`` and the cleared state are what make this a
    test of the guard rather than of the router: a message that reaches
    no handler at all also leaves every rank exactly as it was.

    The demotion lands on rank 3 and not rank 0 on purpose. The target
    guard further down refuses ``level >= actor_rank`` with the SAME
    i18n key, so a rank-0 actor granting rank 2 is refused either way
    and the test would pass with the re-gate deleted. At rank 3 the
    grant of rank 2 clears every downstream guard, which leaves the
    re-gate as the only thing that can produce this refusal.
    """
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_ADMIN, 4), (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)
    _as_chat_admins(bot, monkeypatch, _ADMIN)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack(), user_id=_ADMIN))
    parked = await _state_name(dispatcher, bot, user_id=_ADMIN)
    assert parked == GroupStaffStates.awaiting_grant.state

    # Demoted while the prompt was open. Their Telegram adminship is
    # untouched, so the panel still opens for them — it is the bot rank
    # the write path requires, and that is what just went away.
    await _set_rank(registry, _ADMIN, 3)
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 2", user_id=_ADMIN))

    assert await _ranks(registry) == {_ADMIN: 3, _PLAIN: 0}
    assert t("h_ga_staff_denied", "ru") in _replies(sink)
    assert await _state_name(dispatcher, bot, user_id=_ADMIN) is None


@pytest.mark.asyncio
async def test_a_chat_admin_without_a_bot_rank_cannot_grant_one(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-group escalation: adminship here must not mint ranks.

    The ranks this panel writes are global — rank 2 carries
    ``can_ban``/``can_mute``/``can_kick`` in EVERY group the bot serves
    (``_require_moderation``: "TG-admin OR rank"). Adminship, by
    contrast, is a fact about ONE chat, and anybody can create a group,
    add the bot and be its owner. So the panel opens read-only for
    them: the roster renders, the write path writes nothing.
    """
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)
    _as_chat_admins(bot, monkeypatch, _STRANGER)

    await dispatcher.feed_update(
        bot, _tap(GroupAdminRefresh(section=PAGE_STAFF).pack(), user_id=_STRANGER)
    )
    card = _edits(sink)[-1]["text"]
    assert t("h_ga_staff_readonly", "ru") in card

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack(), user_id=_STRANGER))
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 2", user_id=_STRANGER))

    assert await _ranks(registry) == {_PLAIN: 0}
    # …and for the stated reason: the button itself refused, so the
    # prompt was never armed. Without these two the assertion above
    # would pass just as happily on a typo in the callback data.
    assert t("h_ga_staff_denied", "ru") in _alerts(sink)
    assert await _state_name(dispatcher, bot, user_id=_STRANGER) is None


@pytest.mark.asyncio
async def test_typed_line_without_the_prompt_is_not_staff_input(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0))
    capture_callback_outgoing(bot)

    # No ➕ tap first — an ordinary group message that happens to look
    # like a grant must not change anybody's rank.
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 4"))

    assert await _ranks(registry) == {_PLAIN: 0}


@pytest.mark.asyncio
async def test_pending_prompt_does_not_swallow_commands(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0))
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack()))
    # A command owned by no handler at all: it must fall through to
    # UNHANDLED, not be answered as a malformed grant line. This router
    # sits late in the chain, so without the "/" guard a pending prompt
    # would shadow every command registered after it.
    await dispatcher.feed_update(bot, _say("/unclaimed_command"))
    assert _replies(sink) == []

    # …and the prompt is still armed: the next real line still applies.
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 1"))
    assert await _ranks(registry) == {_PLAIN: 1}


@pytest.mark.asyncio
async def test_cancel_escapes_the_pending_prompt(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    """The prompt advertises ``/cancel``; this pins that the promise holds.

    ``/cancel`` is registered first in the chain and carries no chat-type
    filter, so it reaches the group. After it, the very same line that
    would have been a valid grant must write nothing — the state is gone.
    """
    bot, dispatcher, registry = await _wired(make_wired)
    await _seed(registry, (_PLAIN, 0))
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(GroupAdminStaffAdd().pack()))
    await dispatcher.feed_update(bot, _say("/cancel"))
    await dispatcher.feed_update(bot, _say(f"{_PLAIN} 3"))

    assert await _ranks(registry) == {_PLAIN: 0}
