"""#1983: a bookkeeping ``touch`` held ``users.db`` across the reply.

Nearly every command in this directory opens with
``user_service.touch(...)`` — one UPDATE on ``users``, pure
bookkeeping, and the first write-headed statement of the update. That
is what ``db/engines.py`` promotes to ``BEGIN IMMEDIATE``, so from that
line until the middleware commits after the handler returns, this
update is the only possible writer of ``users.db`` — the busiest file
in the bot. Everything the handler does afterwards happens inside that
window: the other-database reads, the name lookups, and one to three
sequential Telegram calls, each allowed far longer than SQLite's five
second ``busy_timeout``.

``/help`` had this closed in #220 and is the passing control below: the
argument it made there ("the ``touch`` is bookkeeping that stands
either way, so end its transaction here") is not special to a help
screen. It holds for every read-only command that opens the same way,
and the checkpoint costs one commit that was going to happen anyway.

``/city`` is the near miss rather than the omission — it took the
checkpoint in #1934 but placed it just before the geocoder, which is
several early returns too late: the bare form, the reset and the
rejected name all answer above that line and answered under the lock.

Durability proves none of this — the ``touch`` lands either way — so
the probe is the lock itself: a second connection must be able to take
``BEGIN IMMEDIATE`` on ``users.db`` while the handler is mid-send. It
is the same instrument :mod:`tests.e2e.handlers.test_refusal_releases_write_lock`
points at ``economy.db``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import update

from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

USER_ID = 1983

# Write-headed and matching nothing: the promotion to ``BEGIN IMMEDIATE``
# is decided by the statement's head, so a zero-row UPDATE is the cheapest
# way to ask "may I write here right now?".
_USERS_PROBE = update(User).where(User.user_id == -1).values(rank=0)
_ECONOMY_PROBE = update(EconomyUser).where(EconomyUser.user_id == -1).values(balance=0)


def _probe_on(bot: Any, sessionmaker: Any, kinds: tuple[type, ...], statement: Any) -> list[str]:  # noqa: ANN401
    """Wrap the (already stubbed) session so each listed outbound method
    first tries to take that database's writer slot from a second
    connection. A zero-row UPDATE is enough — ``db/engines.py`` promotes
    on the statement's head, not on what it matched.

    Wrapping rather than replacing keeps whichever capture fixture the
    test used in charge of what a call returns. Same instrument as the
    ``economy.db`` helper in
    :mod:`tests.e2e.handlers.test_refusal_releases_write_lock`, taking
    the statement as an argument because this file points it at two
    different databases.
    """
    probe: list[str] = []
    original_request = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ANN401, ASYNC109
        if not isinstance(method, kinds):
            return await original_request(_bot, method, timeout=timeout)
        try:
            async with sessionmaker() as other:
                await other.execute(statement)
                await other.commit()
        except Exception as exc:  # noqa: BLE001 — the verdict IS the exception
            probe.append(repr(exc))
        else:
            probe.append("free")
        return await original_request(_bot, method, timeout=timeout)

    bot.session.make_request = probing
    return probe


# ``(id, text)`` — every private-chat command that opens with ``touch``
# and then talks to Telegram. ``/timezone`` appears three times because
# its three branches differ in what they wrote first: nothing, a clear,
# and a persist. ``/help`` is the control — it has carried the
# checkpoint since #220 and must keep it.
_CASES = [
    ("help", "/help"),
    ("lang", "/lang"),
    ("calc", "/calc 2+2"),
    ("referral", "/referral"),
    ("referrals", "/referrals"),
    ("commission", "/commission"),
    ("timezone_show", "/timezone"),
    ("timezone_reset", "/timezone reset"),
    ("timezone_set", "/timezone Europe/Moscow"),
    ("city_show", "/city"),
    ("city_reset", "/city reset"),
    ("city_bad", "/city ?!"),
    ("achievements", "/achievements"),
    ("start", "/start"),
]


@pytest.mark.parametrize(("case", "text"), _CASES, ids=[c[0] for c in _CASES])
async def test_a_bookkeeping_touch_is_not_held_across_the_reply(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    case: str,
    text: str,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, ModerationBase], session_middleware=True
    )
    sessionmaker = registry.session(DBName.USERS)

    sent = capture_outgoing(bot)
    probe = _probe_on(bot, sessionmaker, (SendMessage,), _USERS_PROBE)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            text,
            user_id=USER_ID,
            chat_id=USER_ID,
            chat_type="private",
            first_name="Лок",
            language_code="ru",
        ),
    )

    assert sent, f"{case}: nothing was sent — the command did not reach its reply"
    assert probe, f"{case}: the probe never fired"
    assert all(verdict == "free" for verdict in probe), (
        f"{case}: users.db was still locked during the reply: {probe}"
    )


async def test_the_language_callback_releases_the_lock_before_both_calls(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The worst of the set: ``touch`` plus the preference write, then a
    toast AND an edit — two round-trips, and a third on the
    ``TelegramBadRequest`` fallback. The write is what the user asked
    for and must stand however the rendering goes, which is exactly the
    condition :class:`db.session.Checkpoint` asks for.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sessionmaker = registry.session(DBName.USERS)

    sent = capture_callback_outgoing(bot)
    probe = _probe_on(bot, sessionmaker, (AnswerCallbackQuery, EditMessageText), _USERS_PROBE)

    await dispatcher.feed_update(bot, make_callback_update("lang_set_en", user_id=USER_ID))

    assert [e for e in sent if e["kind"] == "edit"], "the prompt was never edited"
    assert len(probe) == 2, f"expected a toast and an edit, got {probe}"
    assert all(verdict == "free" for verdict in probe), (
        f"the language callback replied with users.db locked: {probe}"
    )

    # And the choice landed — the checkpoint commits it, it does not
    # discard it.
    async with sessionmaker() as session:
        stored = await session.get(User, USER_ID)
    assert stored is not None


async def test_start_does_not_hold_the_wallet_either(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/start`` is the one case with a second database in the window.

    ``_send_welcome`` seeds the wallet (``get_or_create``, which credits
    the signup balance on a first-ever ``/start``) and only then renders
    and sends. So ``economy.db`` — the file every game and every reward
    writes — was held across the send too, and the checkpoint has to sit
    after the seeding rather than after the ``touch``.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    economy_sessionmaker = registry.session(DBName.ECONOMY)

    sent = capture_outgoing(bot)
    probe = _probe_on(bot, economy_sessionmaker, (SendMessage,), _ECONOMY_PROBE)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/start",
            user_id=USER_ID,
            chat_id=USER_ID,
            chat_type="private",
            first_name="Лок",
            language_code="ru",
        ),
    )

    assert sent, "/start sent no welcome"
    assert probe == ["free"], f"/start sent the welcome with economy.db locked: {probe}"

    # The seeded wallet is still there afterwards.
    async with economy_sessionmaker() as session:
        wallet = await session.get(EconomyUser, USER_ID)
    assert wallet is not None
