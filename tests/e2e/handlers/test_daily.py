"""End-to-end ``/daily`` flow: dispatcher → EconomyMiddleware → claim pipeline.

Stage 13 wires the handler over Stages 8-12 (DailyService, EffectsService,
PrivilegesRepo, VipRepo). The matrix here pins:

* Happy path — first claim seeds wallet, credits, renders the success
  card with the post-claim balance.
* Cooldown — second claim within 24h renders the cooldown card and
  does NOT credit the wallet again (regression guard against a future
  refactor that drops the SQL race guard in
  :meth:`EconomyRepo.mark_daily_claimed`).
* VIP percent — an active ``vip_till`` adds the legacy 15% bonus
  end-to-end (covers EffectsService → VipRepo → reward math).
* Double-daily buster — present on success, consumed on success
  (covers ``EffectsService.consume_double_daily`` invariant); the
  same buster expired in the past must NOT double.
* Anonymous sender_chat — rejected without touching the wallet.
* Group chats get the #123 private-only refusal (router-level filter).
* Args on /daily fall through (no current alias support).
* #1861 — the claim is durable when the success card cannot be
  delivered: the write transaction ends before the send, so a
  kicked/blocked bot no longer silently un-claims the day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, UserPrivilege
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    assert_unknown_form_hint,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot
    from aiogram.types import Update

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, user_id: int = 777, chat_type: str = "private") -> Update:
    return make_message_update(
        text,
        user_id=user_id,
        first_name="Eve",
        language_code="ru",
        chat_type=chat_type,
    )


async def test_daily_first_claim_credits_wallet_and_renders_success(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """First /daily ever: wallet auto-created (welcome credit 100),
    claim credits +base reward, success card shows new balance."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/daily"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Ежедневный бонус получен" in body
    assert "Streak" in body

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(777)
    assert wallet is not None
    # Welcome credit (100) + at least the daily floor (random_min=1).
    assert wallet.balance >= 101
    assert wallet.daily_streak == 1
    assert wallet.last_daily is not None


async def test_daily_second_claim_within_24h_renders_cooldown(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """SQL race guard: a back-to-back claim must surface as cooldown,
    not as a silent double-credit. Pins the
    ``julianday(now) - julianday(last_daily) >= 1`` invariant
    end-to-end."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/daily"))
    first_balance_sessionmaker = registry.session(DBName.ECONOMY)
    async with first_balance_sessionmaker() as session:
        first = await EconomyRepo(session).get(777)
    assert first is not None
    after_first = first.balance

    await dispatcher.feed_update(bot, _update("/daily"))
    assert len(sent) == 2
    assert "Бонус уже получен" in sent[1]["text"]

    async with first_balance_sessionmaker() as session:
        second = await EconomyRepo(session).get(777)
    assert second is not None
    assert second.balance == after_first  # no double credit
    assert second.daily_streak == first.daily_streak


async def test_daily_with_active_vip_applies_15_percent_bonus(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active global VIP grant must surface as ``vip_percent=15``
    through EffectsService → DailyService and lift the credited amount
    by 15% relative to the no-VIP case.

    Previously this test was xfail/flaky because it asserted
    ``balance >= 6`` against a randomised base in ``[1, 50]`` —
    ``daily_reward(base=base, streak=1, streak_bonus=5, vip_percent=15)``
    can be as low as 1 (when the random roll lands on 1), so the
    assertion failed ~half the time. The VIP wiring itself was
    working; the test just wasn't deterministic. D-001 (2026-05-25):
    pin the random base via ``monkeypatch`` to make the assertion
    exact instead of probabilistic. With ``base=20`` the VIP bonus
    is ``int(20 * 0.15) = 3``, so total ``daily_reward`` = 23 (vs
    20 without VIP) — a 1:1 evidence that the 15% lifted through.
    """
    # Patch the random-base chooser at the import site
    # (services.daily_service) so DailyService.roll_base_reward
    # always sees a fixed base regardless of the per-request
    # ``random.Random()`` instance the middleware constructs.
    # Returning ``([20], [1])`` makes ``choices`` pick 20 every
    # time — base=20 is large enough that 15% rounds to a
    # non-zero integer (int(20*0.15)=3), unlike base<7 where the
    # VIP bonus floors to 0 and erases the signal we're testing.
    monkeypatch.setattr(
        "telegram_invite_bot.services.daily_service.weighted_random_choices",
        lambda _min, _max: ([20], [1]),
    )

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    sessionmaker = registry.session(DBName.ECONOMY)
    future = datetime(2099, 1, 1, tzinfo=UTC).timestamp()
    async with sessionmaker() as session:
        # Pre-seed VIP wallet to skip welcome credit randomness.
        session.add(EconomyUser(user_id=777, balance=0, language="ru", vip_till=future))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/daily"))
    assert "Ежедневный бонус получен" in sent[0]["text"]

    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(777)
    assert wallet is not None
    # Exact expected payout per daily_reward formula:
    # base + (streak-1)*streak_bonus + int(payout * vip_pct / 100)
    # = 20 + 0*5 + int(20 * 15/100) = 20 + 3 = 23.
    # If the VIP grant doesn't surface, EffectsService would pass
    # vip_percent=0 and balance would be 20 — failing this assertion
    # loudly instead of flaking on RNG.
    assert wallet.balance == 23, (
        f"VIP bonus did not apply: expected 23 (base 20 + 15%=3), "
        f"got {wallet.balance}. If 20, the VIP wiring is broken; "
        f"otherwise the formula or random patch drifted."
    )


async def test_daily_double_buster_consumed_on_success(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Active ``double_daily`` row → reward doubled AND the row is
    deleted on success. Pins the consume-on-success invariant
    documented in the handler."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=777, balance=0, language="ru"))
        session.add(UserPrivilege(user_id=777, privilege_type="double_daily", group_id=0))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/daily"))
    assert "Ежедневный бонус получен" in sent[0]["text"]

    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(777)
        # Buster row must be gone.
        from sqlalchemy import select

        rows = (
            await session.execute(
                select(UserPrivilege).where(
                    UserPrivilege.user_id == 777,
                    UserPrivilege.privilege_type == "double_daily",
                )
            )
        ).all()
    assert wallet is not None
    assert wallet.balance >= 2  # x2 of the minimum daily floor (1)
    assert rows == []


async def test_daily_expired_double_buster_does_not_double_and_is_not_consumed(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Expired buster row stays in the DB (PrivilegesRepo.get_active
    filters on expiry; the handler never sees ``double=True``, so
    consume_double_daily is never called)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    sessionmaker = registry.session(DBName.ECONOMY)
    past = (datetime.now(tz=UTC) - timedelta(days=1)).timestamp()
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=777, balance=0, language="ru"))
        session.add(
            UserPrivilege(
                user_id=777,
                privilege_type="double_daily",
                group_id=0,
                expires_at=past,
            )
        )
        await session.commit()

    capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/daily"))

    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = (
            await session.execute(select(UserPrivilege).where(UserPrivilege.user_id == 777))
        ).all()
    # Expired row preserved — cleanup is a future cron's job, not the
    # read path's; pinned so a future "delete on read" optimisation
    # surfaces here.
    assert len(rows) == 1


async def test_daily_in_group_chat_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A group ``/daily`` is answered rather than swallowed (#123).

    The group flow still owns ``ensure_user_access`` + per-chat economy
    gating, neither of which is ported, so the router-level private
    filter keeps the claim pipeline out exactly as before. What changed
    is the silence: the refusal twin now points the user at the DM.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/daily", chat_type="supergroup"))

    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="daily")


async def test_daily_with_args_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/daily anything`` — the claim handler still declines it (#158).

    This asserted ``UNHANDLED`` and an empty sink while legacy owned
    every off-contract form and answered with its own hint. Legacy is
    gone, so falling through stopped meaning "someone else replies" and
    started meaning "nobody replies". The claim path is unchanged — the
    bonus is still only granted by the bare form; what changed is that
    a typo now gets an answer instead of nothing.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/daily foo"))

    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="daily")


async def test_daily_at_balance_ceiling_keeps_the_day(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A wallet at ``_MAX_AMOUNT`` can't be credited — the day must survive.

    The claim marks ``last_daily`` before the credit, so without the
    service-side rollback the middleware would commit a consumed day
    with no coins. End-to-end guard: the card explains it, and a claim
    after the balance drops still works.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT  # noqa: PLC0415

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=777, balance=_MAX_AMOUNT, language="ru"))
        await session.commit()

    result = await dispatcher.feed_update(bot, _update("/daily"))
    assert result is not UNHANDLED
    assert "баланс уже на максимуме" in sent[-1]["text"]

    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(777)
    assert wallet is not None
    assert wallet.balance == _MAX_AMOUNT
    assert wallet.last_daily is None  # not burned
    assert wallet.daily_streak == 0

    # Room again → the same day's bonus is still claimable.
    async with sessionmaker() as session:
        await session.execute(
            EconomyUser.__table__.update().where(EconomyUser.user_id == 777).values(balance=100)
        )
        await session.commit()

    await dispatcher.feed_update(bot, _update("/daily"))
    assert "Ежедневный бонус получен" in sent[-1]["text"]


# ---------------------------------------------------------------------------
# #1861 — the write lock must not span the outgoing card
# ---------------------------------------------------------------------------


async def test_daily_claim_is_durable_when_the_success_card_cannot_be_sent(
    make_wired: WiredFactory,
) -> None:
    """#1861: ``EconomyMiddleware`` binds every economy repo to ONE
    session, so ``mark_daily_claimed`` → ``credit`` →
    ``award_achievements`` → ``consume_double_daily`` are a single
    transaction that stays open — holding ``economy.db``'s
    ``BEGIN IMMEDIATE`` — across ``message.answer``.

    Two things go wrong at once when that send fails. The lock is held
    for the whole round trip (SQLite's ``busy_timeout`` is 5s, a
    Telegram call can outlive it), and the middleware rolls the claim
    back on the raise — so the coins the user earned evaporate along
    with the card that would have told them. The checkpoint ends the
    transaction first; the raise then loses only the card.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage
    from sqlalchemy import select

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=777, balance=0, language="ru"))
        session.add(UserPrivilege(user_id=777, privilege_type="double_daily", group_id=0))
        await session.commit()

    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]

    # The dispatcher's own errors router swallows the raise (it reads
    # ``TelegramForbiddenError`` as a benign reject and stays quiet), so
    # nothing surfaces here — which is exactly why the rollback was
    # invisible: the user lost the claim and no one was told.
    await dispatcher.feed_update(bot, _update("/daily"))

    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(777)
        rows = (
            await session.execute(
                select(UserPrivilege).where(
                    UserPrivilege.user_id == 777,
                    UserPrivilege.privilege_type == "double_daily",
                )
            )
        ).all()
    assert wallet is not None
    # The day is burned and paid for: streak advanced, coins credited.
    assert wallet.daily_streak == 1
    assert wallet.last_daily is not None
    assert wallet.balance >= 2  # x2 of the minimum daily floor (1)
    # The buster was spent on that claim and must not come back either;
    # a checkpoint placed before ``consume_double_daily`` would leave
    # the user paid AND still holding the item.
    assert rows == []


async def test_race_lost_releases_the_write_lock_before_replying(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1861, the branch with nothing to commit but a lock to drop.

    ``RACE_LOST`` means ``mark_daily_claimed``'s guarded UPDATE ran and
    matched zero rows — a write-headed statement, so ``db/engines.py``
    still promoted the transaction to ``BEGIN IMMEDIATE``. Nothing is
    pending, yet ``economy.db`` stays locked for the whole cooldown
    card. Durability cannot show this (there is nothing to be durable
    about), so probe the lock itself: a second connection must be able
    to take ``BEGIN IMMEDIATE`` while the handler is mid-send.

    The race is reproduced the way it actually happens — the row on
    disk is fresh, but the wallet this claim read still says the day is
    free, so the Python check waves it through and the SQL guard is the
    one that says no.
    """
    from dataclasses import replace

    from aiogram.methods import SendMessage
    from sqlalchemy import update

    from telegram_invite_bot.core.entities.wallet import Wallet

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)

    async with sessionmaker() as session:
        session.add(
            EconomyUser(
                user_id=777,
                balance=0,
                language="ru",
                # Fresh on disk: the SQL guard's julianday window has
                # not passed, so its UPDATE will match nothing.
                last_daily=datetime.now(tz=UTC).replace(tzinfo=None),
                daily_streak=1,
            )
        )
        await session.commit()

    original_get = EconomyRepo.get

    async def stale_get(self: EconomyRepo, user_id: int) -> Wallet | None:
        """What the losing claim saw: a wallet read before the winner wrote."""
        wallet = await original_get(self, user_id)
        return None if wallet is None else replace(wallet, last_daily=None)

    EconomyRepo.get = stale_get  # type: ignore[method-assign]

    probe: list[str] = []
    # Wrap the capture fixture's stub, not the live session — calling
    # through to the real transport would put a request on the wire.
    sent = capture_outgoing(bot)
    original_request = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        # A second connection to the same file. A write-headed statement
        # needs the same lock the handler may still be holding; SQLite
        # waits out ``busy_timeout`` (5s) before giving up.
        if not isinstance(method, SendMessage):
            return await original_request(_bot, method, timeout=timeout)
        try:
            async with sessionmaker() as other:
                await other.execute(
                    update(EconomyUser).where(EconomyUser.user_id == -1).values(balance=0)
                )
                await other.commit()
        except Exception as exc:  # noqa: BLE001 — the verdict IS the exception
            probe.append(repr(exc))
        else:
            probe.append("free")
        return await original_request(_bot, method, timeout=timeout)

    bot.session.make_request = probing  # type: ignore[method-assign,assignment]
    try:
        await dispatcher.feed_update(bot, _update("/daily"))
    finally:
        EconomyRepo.get = original_get  # type: ignore[method-assign]

    assert probe == ["free"], f"economy.db was still locked during the send: {probe}"
    assert len(sent) == 1
    assert "Бонус уже получен" in sent[0]["text"]

    # And the guard did its job: the losing claim wrote nothing.
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(777)
    assert wallet is not None
    assert wallet.daily_streak == 1
