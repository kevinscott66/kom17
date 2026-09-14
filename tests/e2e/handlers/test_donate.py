"""End-to-end ``/donate`` (#2007) — a coin donation to the current group.

Legacy ``cmd_donate`` (bot.py:24872) let a member donate coins to the
group they are writing in: the sum becomes ``groups_donations.group_xp``
— the score ``/rating`` ranks groups by — and the group's creator is
paid that sum minus the developer commission. T-011 removed legacy and
the port replaced the command with a static "support the bot" blurb in
``handlers/support.py``, registered PRIVATE-only. So the surface the
catalog still advertises as «поддержать группу или автора монетами»
(``h_cmd_donate``) answered, in a group, with the #123 "this works in a
DM" refusal, and in the DM with a blurb pointing at a donation link no
setting in this package can hold. Two other live cards
(``no_groups_with_donates``, ``h_donaters_empty``) point the user at
``/donate`` as the way to fund a group, so the dead end was reachable
from the product itself, not only from ``/help``.

Pins here:

* the money actually moves — donor debited, group xp up, owner paid the
  remainder after the developer cut, ledger rows for all three;
* the group ledger tables legacy wrote are all written
  (``donations``, ``group_top_donators``, ``groups_donations``);
* a group whose creator cannot be resolved still climbs the board and
  the receipt does not promise a payout nobody got;
* every refusal is a refusal — bounds, balance, anonymous admin,
  cooldown — and none of them writes anything;
* the receipt reports ``group_xp``, which is what the leaderboard ranks
  on. Legacy printed ``total_donations`` here (bot.py:24906) — a column
  nothing has incremented since before the cutover (bot.py:10915
  «Сейчас не пополняется»), so its own success card always showed a
  number that did not move;
* a DM ``/donate`` gets the group-only refusal, not the old blurb.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import ChatMemberOwner, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr
from sqlalchemy import select, text

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.models.economy import (
    Donation,
    EconomyUser,
    GroupDonationsAggregate,
    GroupTopDonator,
    Transaction,
)

# Importing the rank tables registers them on ModerationBase.metadata —
# the command-access middleware reads its overrides on every command.
from telegram_invite_bot.db.models.rank_tables import (  # noqa: F401
    CommandRankOverride,
    RankPermissionOverride,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.economy_repo import WELCOME_BALANCE
from tests.e2e.handlers.conftest import assert_chat_scope_refusal, make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

pytestmark = pytest.mark.asyncio

CHAT_ID = -100_555
DONOR_ID = 4242
OWNER_ID = 9001
# The developer commission is paid to ``ADMIN_CHAT_ID`` (legacy dev_id,
# bot.py:9888) — not to ``DEVELOPER_ID_1``, which only grants the bypass.
ADMIN_ID = 777

# The coin glyph every economy card renders (handlers/group_pay.py).
_COIN = "🪙"

# ``rating_history`` ships in migration 0009 as a raw-SQL table (no ORM
# model), so ``create_all(EconomyBase)`` doesn't produce it — same
# fixture shape as tests/e2e/handlers/test_group_pay.py. The donation
# writes today's snapshot into it, so it has to exist for the happy path.
_RATING_HISTORY_DDL = (
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")"
)


async def _seed(registry: EngineRegistry, *, balance: int) -> None:
    async with registry.session(DBName.ECONOMY)() as session:
        await session.execute(text(_RATING_HISTORY_DDL))
        session.add(EconomyUser(user_id=DONOR_ID, balance=balance))
        await session.commit()


def _update(body: str = "/donate 100", **kwargs: Any) -> Update:
    kwargs.setdefault("user_id", DONOR_ID)
    kwargs.setdefault("chat_id", CHAT_ID)
    kwargs.setdefault("chat_type", "supergroup")
    return make_message_update(body, **kwargs)


def _anonymous_update(body: str = "/donate 100") -> Update:
    """A message sent on behalf of the chat itself.

    ``make_message_update`` has no ``sender_chat`` knob (there is one
    other caller that needs it — ``test_marriage.py`` — and it builds
    the payload by hand for the same reason). ``from`` is the
    ``GroupAnonymousBot`` account Telegram substitutes, so the update
    still passes the ``F.from_user`` filter: the refusal has to come
    from the handler, not from the router.
    """
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": CHAT_ID, "type": "supergroup", "title": "G"},
                "from": {"id": 1087968824, "is_bot": True, "first_name": "Group"},
                "sender_chat": {"id": CHAT_ID, "type": "supergroup", "title": "G"},
                "text": body,
                "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
            },
        }
    )


def _with_creator(bot: Bot, creator_id: int = OWNER_ID) -> None:
    """Make ``chat_creator_id`` resolve.

    The conftest stub answers ``GetChatAdministrators`` with an empty
    list (nobody is the creator), which is the ownerless branch — the
    payout half needs a real one. Must be installed AFTER
    ``capture_outgoing``, whose patch this one delegates to, and with
    its exact signature.
    """
    original = bot.session.make_request

    async def patched(bot_: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "GetChatAdministrators":
            return [
                ChatMemberOwner(
                    user=TelegramUser(id=creator_id, is_bot=False, first_name="Owner"),
                    is_anonymous=False,
                )
            ]
        return await original(bot_, method, timeout)

    bot.session.make_request = patched  # type: ignore[method-assign]


async def _state(registry: EngineRegistry) -> dict[str, Any]:
    async with registry.session(DBName.ECONOMY)() as session:
        balances = {
            row[0]: row[1]
            for row in (
                await session.execute(select(EconomyUser.user_id, EconomyUser.balance))
            ).all()
        }
        donations = (await session.execute(select(Donation))).scalars().all()
        top = (await session.execute(select(GroupTopDonator))).scalars().all()
        xp = (
            await session.execute(
                select(GroupDonationsAggregate.group_xp).where(
                    GroupDonationsAggregate.group_id == CHAT_ID
                )
            )
        ).scalar_one_or_none()
        ledger = (
            (await session.execute(select(Transaction).order_by(Transaction.id))).scalars().all()
        )
    return {
        "balances": balances,
        "donations": [(d.user_id, d.group_id, d.amount) for d in donations],
        "top": [(r.group_id, r.user_id, r.total_donated) for r in top],
        "xp": xp,
        "ledger": [(r.from_id, r.to_id, r.amount, r.type) for r in ledger],
    }


async def test_donate_moves_the_coins_and_ranks_the_group(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, ModerationBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), ADMIN_CHAT_ID=ADMIN_ID),
    )
    await _seed(registry, balance=500)
    sent = capture_outgoing(bot)
    _with_creator(bot)

    result = await dispatcher.feed_update(bot, _update("/donate 100"))

    assert result is not UNHANDLED
    state = await _state(registry)
    # Donor paid exactly the sum they named.
    assert state["balances"][DONOR_ID] == 400
    # The group climbed the board by the FULL sum (not the net payout).
    assert state["xp"] == 100
    assert state["donations"] == [(DONOR_ID, CHAT_ID, 100)]
    assert state["top"] == [(CHAT_ID, DONOR_ID, 100)]
    # Owner gets the sum minus the 5% developer cut; the cut goes to the
    # admin wallet. Both are MINTS (``from_id`` is None), like every
    # other group payout in this package; the donor's side is a spend.
    # Neither had a wallet before the payout, and a wallet minted on
    # first touch opens at the welcome balance rather than at zero
    # (``EconomyRepo.get_or_create``) — hence the offset, not a bare 95.
    assert state["balances"][OWNER_ID] == WELCOME_BALANCE + 95
    assert state["balances"][ADMIN_ID] == WELCOME_BALANCE + 5
    assert state["ledger"] == [
        (DONOR_ID, 0, -100, "donate"),
        (None, OWNER_ID, 95, "donate_to_owner"),
        (None, ADMIN_ID, 5, "donate_commission"),
    ]
    assert "100" in sent[-1]["text"]


async def test_donate_reports_the_score_the_leaderboard_ranks_on(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Legacy's receipt read ``total_donations`` (bot.py:24906), which no
    writer has touched since before the cutover, so a group with 700 xp
    and a frozen treasury was told its donations totalled the treasury.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=500)
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(GroupDonationsAggregate(group_id=CHAT_ID, total_donations=7777, group_xp=600))
        await session.commit()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/donate 100"))

    body = sent[-1]["text"]
    assert "700" in body
    assert "7777" not in body


async def test_donate_without_an_owner_still_ranks_the_group(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The conftest stub answers ``GetChatAdministrators`` with an empty
    list, so no creator resolves — legacy's ``if owner_id:`` skip
    (bot.py:10747). The xp is never conditional on the payout.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/donate 100"))

    state = await _state(registry)
    assert state["balances"][DONOR_ID] == 400
    assert state["xp"] == 100
    # Nobody but the donor has a wallet — no payout was invented.
    assert set(state["balances"]) == {DONOR_ID}
    # And the receipt does not claim one.
    assert "95" not in sent[-1]["text"]


async def test_donate_refuses_more_than_the_balance_and_writes_nothing(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=10)
    sent = capture_outgoing(bot)
    _with_creator(bot)

    await dispatcher.feed_update(bot, _update("/donate 100"))

    state = await _state(registry)
    assert state["balances"][DONOR_ID] == 10
    assert state["donations"] == []
    assert state["top"] == []
    assert state["xp"] is None
    assert state["ledger"] == []
    # The card names what the user actually has, so the next attempt
    # doesn't have to be a guess.
    assert "10" in sent[-1]["text"]


async def test_donate_enforces_the_configured_bounds(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=10_000_000)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/donate 0"))
    await dispatcher.feed_update(bot, _update("/donate 999999999", message_id=2, update_id=2))

    state = await _state(registry)
    assert state["donations"] == []
    assert state["balances"][DONOR_ID] == 10_000_000
    # Two DIFFERENT refusals, each naming the bound it enforces — a
    # single shared "wrong amount" line would leave the user guessing
    # which end they hit.
    assert [e["text"] for e in sent] == [
        t("h_donate_min", "ru", min=1, sign="\U0001fa99"),
        t("h_donate_max", "ru", max=1_000_000, sign="\U0001fa99"),
    ]


async def test_donate_refuses_an_anonymous_admin(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The debit lands on a PERSON's wallet; a message sent on behalf of
    the chat has no person behind it — the ``from`` id Telegram
    substitutes is a shared bot account, so a donation charged to it
    would be charged to whichever anonymous admin happened to type it.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _anonymous_update("/donate 100"))

    state = await _state(registry)
    assert state["balances"][DONOR_ID] == 500
    assert state["donations"] == []
    assert state["ledger"] == []
    assert [e["text"] for e in sent] == [t("h_donate_anonymous", "ru")]


async def test_donate_without_an_amount_shows_the_usage_card(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=500)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/donate"))

    assert result is not UNHANDLED
    state = await _state(registry)
    assert state["donations"] == []
    # The card tells the user their balance and the accepted range,
    # rather than a bare "wrong syntax".
    body = sent[-1]["text"]
    assert "500" in body
    assert "/donate" in body


async def test_second_donation_inside_the_cooldown_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Legacy stamps the cooldown only on SUCCESS (bot.py:24902), so a
    typo never costs the user their next ten seconds.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=500)
    sent = capture_outgoing(bot)

    # A refusal first: it must NOT start the clock.
    await dispatcher.feed_update(bot, _update("/donate 0"))
    await dispatcher.feed_update(bot, _update("/donate 100", message_id=2, update_id=2))
    await dispatcher.feed_update(bot, _update("/donate 100", message_id=3, update_id=3))

    state = await _state(registry)
    assert state["balances"][DONOR_ID] == 400
    assert state["donations"] == [(DONOR_ID, CHAT_ID, 100)]
    assert len(sent) == 3


class _ParkedCreatorLookup:
    """``chat_creator_id`` that freezes its first caller (#2016).

    Same shape as :func:`_with_creator` — it wraps the conftest stub and
    answers ``GetChatAdministrators`` with a real owner — but the first
    lookup parks until released. That call is the first await past the
    cooldown gate, so freezing it holds one ``/donate`` exactly where a
    burst of them used to overtake it.
    """

    def __init__(self, bot: Bot, creator_id: int = OWNER_ID) -> None:
        self._original = bot.session.make_request
        self._creator_id = creator_id
        self.armed = True
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        bot.session.make_request = self  # type: ignore[assignment]

    async def __call__(self, bot_: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "GetChatAdministrators":
            if self.armed:
                self.armed = False
                self.parked.set()
                # Bounded so a regression can never hang the suite.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.release.wait(), timeout=30.0)
            return [
                ChatMemberOwner(
                    user=TelegramUser(id=self._creator_id, is_bot=False, first_name="Owner"),
                    is_anonymous=False,
                )
            ]
        return await self._original(bot_, method, timeout)


async def test_donations_arriving_during_one_donation_share_its_cooldown(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """#2016: the anti-spam gap must be claimed with the decision to spend it.

    The gate read the cooldown table at the top of the handler and
    stamped it at the very bottom, past ``getChatAdministrators`` and
    the whole donation transaction. Every ``/donate`` sent inside that
    window read a clear table, so the gap the owner configured bounded
    nothing: a donor tapping the command three times moved three lots of
    coins and wrote three ledger rows.

    Driven rather than raced: the first donation is frozen inside its
    owner lookup, two more are fed and allowed to run as far as they
    can, and only then is the first released.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    await _seed(registry, balance=500)
    sent = capture_outgoing(bot)
    gate = _ParkedCreatorLookup(bot)

    winner = asyncio.create_task(dispatcher.feed_update(bot, _update("/donate 100")))
    await gate.parked.wait()
    losers = [
        asyncio.create_task(
            dispatcher.feed_update(bot, _update("/donate 100", message_id=n, update_id=n))
        )
        for n in (2, 3)
    ]
    await asyncio.wait(set(losers), timeout=1.0)
    gate.release.set()
    await asyncio.gather(winner, *losers)

    state = await _state(registry)
    assert state["donations"] == [(DONOR_ID, CHAT_ID, 100)], (
        f"the burst bought {len(state['donations'])} donations on one cooldown"
    )
    assert state["balances"][DONOR_ID] == 400
    assert len(sent) == 3, f"one receipt and two refusals were expected, got: {sent}"


async def test_donate_in_a_dm_gets_the_group_only_refusal(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Not the old ``h_donate_blurb``: the money lands on a group, so a
    DM has no group to land it on.
    """
    bot, dispatcher, _registry = await make_wired(schemas=[UsersBase, EconomyBase, ModerationBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, make_message_update("/donate 100", user_id=DONOR_ID, chat_id=DONOR_ID)
    )

    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="donate")
