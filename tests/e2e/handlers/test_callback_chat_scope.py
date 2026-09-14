"""#1608: chat-type scope on the CALLBACK side of private-only routers.

The audit that filed #1608 measured 10 of 30 callback-registering
modules carrying a ``callback_query.filter``. Every one of the other
twenty was inert — the payloads are only ever rendered in private and
the handlers re-scope on ``callback.from_user.id`` — but that is the
same "the inner gate will catch it" reasoning that had to be repaired
three times already, so the scope is now stated on the router itself
wherever the card provably belongs to one chat type.

Pins here, in two halves:

* Three routers gained the filter — ``topup``, ``withdraw``,
  ``mygroups``. A callback fed from a supergroup no longer reaches
  the handler: it falls past the owning router to #159's stale-card
  tail, which answers the spinner and renders nothing. That tail is
  what makes the pin sharp — with the filter removed the handler
  consumes the click and the toast never appears, so the assertion
  fails from both directions. The ``withdraw`` case deliberately
  pre-seeds the GROUP FSM key so its ``StateFilter`` passes:
  without that seeding the per-chat storage key alone would reject
  the update and the test would be green with or without the fix.
* Four routers are deliberate EXEMPTIONS and must stay unfiltered:
  ``p2p_trade`` (the dispute card's resolve buttons live in the admin
  chat, usually a group — a private-only filter would break dispute
  resolution, a money path), ``support`` (``/faq`` worked in groups in
  legacy and its FaqContinue pager must follow it there), ``language``
  (the legacy in-group ``/lang`` keyboard fires ``lang_set_*`` and this
  handler owns those clicks on every surface) and ``shop`` (a stray
  click on a forwarded button should still resolve through the new
  pipeline). This half exists so a later "uniformity" sweep cannot
  quietly turn a documented exemption into a regression.

The exemption half reads ``callback_query._handler.filters`` — the
router-level filter list. ``Router.callback_query.filters`` does not
exist as a public accessor and ``check_root_filters`` would need a real
event; the private attribute is the exact list ``.filter()`` appends to
and ``SLF`` is not in the ruff select list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Router

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.fsm.withdraw import WithdrawStates
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import WithdrawConfirm
from telegram_invite_bot.keyboards.builders.mygroups import MyGroupsPage
from telegram_invite_bot.keyboards.builders.topup import TopupMethod
from tests.e2e.handlers.conftest import make_callback_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory

_GROUP = -100_555
_UID = 42

_FILTERED = ("topup", "withdraw", "mygroups")
_EXEMPT = ("p2p_trade", "support", "language", "shop")


def _routers_by_name(root: Router) -> dict[str, Router]:
    """Flatten the wired tree. ``with_chat_type_refusal`` inserts a
    ``<name>:scoped`` ancestor around several of these; the worker keeps
    the bare name and is the router the filters were set on.
    """
    found: dict[str, Router] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        found[node.name] = node
        stack.extend(node.sub_routers)
    return found


def _assert_only_the_stale_toast(sent: list[dict[str, Any]]) -> None:
    """The click was declined by its owning router and answered by
    #159's tail: one ``answerCallbackQuery`` carrying the stale-card
    text, and no ``edit`` — the card itself never rendered.
    """
    assert [e["kind"] for e in sent] == ["callback_answer"], sent
    assert sent[0]["text"] in {t("h_stale_card", "ru"), t("h_stale_card", "en")}


@pytest.mark.asyncio
async def test_topup_callback_from_a_group_falls_through(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            TopupMethod(method="stars").pack(),
            user_id=_UID,
            chat_id=_GROUP,
            chat_type="supergroup",
        ),
    )
    _assert_only_the_stale_toast(sent)


@pytest.mark.asyncio
async def test_mygroups_callback_from_a_group_falls_through(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            MyGroupsPage(page=2).pack(),
            user_id=_UID,
            chat_id=_GROUP,
            chat_type="supergroup",
        ),
    )
    _assert_only_the_stale_toast(sent)


@pytest.mark.asyncio
async def test_withdraw_confirm_from_a_group_falls_through_even_in_state(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    """The group FSM key is put INTO ``awaiting_confirm`` first.

    Without that seeding the ``StateFilter`` on the confirm handler
    would reject the update on its own and the test would pass with the
    filter removed — measuring the storage key, not the chat scope.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase])
    context = dispatcher.fsm.get_context(bot, chat_id=_GROUP, user_id=_UID)
    await context.set_state(WithdrawStates.awaiting_confirm)
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            WithdrawConfirm(user_id=_UID).pack(),
            user_id=_UID,
            chat_id=_GROUP,
            chat_type="supergroup",
        ),
    )
    _assert_only_the_stale_toast(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _FILTERED)
async def test_router_carries_a_callback_chat_filter(make_wired: WiredFactory, name: str) -> None:
    _bot, dispatcher, _registry = await make_wired()
    router = _routers_by_name(dispatcher)[name]
    assert router.callback_query._handler.filters, (
        f"{name}: #1608 put a chat-type filter on the callback chain; "
        "removing it silently widens the card's scope back to any chat"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _EXEMPT)
async def test_exempt_router_stays_unfiltered(make_wired: WiredFactory, name: str) -> None:
    """Recorded #1608 exemptions — see this module's docstring for why
    each one must keep answering clicks that arrive from a group.
    """
    _bot, dispatcher, _registry = await make_wired()
    router = _routers_by_name(dispatcher)[name]
    assert not router.callback_query._handler.filters, (
        f"{name}: this router is a DOCUMENTED #1608 exemption. Adding a "
        "private-only callback filter here breaks a real flow — read the "
        "rationale in its build_router docstring before changing it"
    )
