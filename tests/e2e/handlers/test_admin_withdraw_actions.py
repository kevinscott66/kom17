"""End-to-end ``/admin_withdrawals`` approve/reject actions (L-97).

The read-only card (``test_admin_withdrawals.py``) renders the queue
with a ✅/🚫 button pair per pending row; clicking drives
:class:`WithdrawService`:

* ✅ approve → manual-payout doctrine v1: the row flips to ``completed``
  with ``tx_hash="manual"`` and NO Crypto Pay call of any kind — the
  operator pays the user out-of-band, exactly like legacy
  ``admin_confirm_withdrawal`` (bot.py:20717). The user gets a
  localized DM, the operator a toast, the card refreshes;
* 🚫 reject → refund the escrowed coins to the user's wallet, flip the
  row to ``rejected``, DM the user, toast the operator;
* ``/admin_withdrawals reject <id> [reason]`` → same refund path with
  an operator reason stored in ``admin_note`` and rendered
  (HTML-escaped) into the user's DM;
* a non-developer click is silently dropped (no DB change, no DM).

#1507: the user's DM language comes from ``user_settings.language`` in
users.db — where ``/lang`` writes — and NOT from
``economy.users.language``, which ``EconomyRepo.get_or_create`` stamps
once under ``ON CONFLICT DO NOTHING`` and never refreshes. The two
DM-language cases below seed the wallet column with the WRONG language
on purpose, so a resolver that read it would fail them.

No network seam is needed anymore: the manual approve path makes zero
HTTP calls, and the capture fixtures assert exactly that by raising on
any unexpected outbound method.

DM/toast assertions go through ``t(key, lang, ...)`` rather than literal
strings: the equality pins the handler→key wiring and the formatting
arguments, which is what a refactor breaks, while leaving the wording
free to change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, WithdrawalRequest
from telegram_invite_bot.db.models.user_settings import UserSetting
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import WithdrawApprove, WithdrawReject
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_DEV = 555
_USER = 4242


async def _seed_wallet(registry: EngineRegistry, user_id: int, *, balance: int, lang: str) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language=lang))
        await session.commit()


async def _seed_language(registry: EngineRegistry, user_id: int, language: str) -> None:
    """Record an explicit ``/lang`` choice — the only language source
    the DM path is allowed to trust (#1507).

    ``user_settings.user_id`` is a real FK onto ``users.user_id`` and
    the registry runs with ``PRAGMA foreign_keys=ON``, so the parent row
    has to be committed before the child one.
    """
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(User(user_id=user_id, first_name="T"))
        await session.commit()
        session.add(UserSetting(user_id=user_id, language=language))
        await session.commit()


async def _seed_request(registry: EngineRegistry, **cols: Any) -> int:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = WithdrawalRequest(**cols)
        session.add(row)
        await session.commit()
        return int(row.id)


async def _row(registry: EngineRegistry, request_id: int) -> WithdrawalRequest | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        res = await session.execute(
            select(WithdrawalRequest).where(WithdrawalRequest.id == request_id)
        )
        return res.scalar_one_or_none()


async def _balance(registry: EngineRegistry, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        res = await session.execute(
            select(EconomyUser.balance).where(EconomyUser.user_id == user_id)
        )
        val = res.scalar_one_or_none()
        return int(val) if val is not None else None


@pytest.mark.asyncio
async def test_approve_marks_completed_manual_payout(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """✅ approve: the row flips to completed with ``tx_hash='manual'``
    and the user gets a localized DM. NO provider call fires — the
    capture fixture raises on any non-send/edit/answer method, so a
    sneaky Crypto Pay HTTP attempt would fail this test by itself."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    # Wallet column deliberately disagrees with the /lang choice: it is
    # the stale one, and must lose (#1507).
    await _seed_wallet(registry, _USER, balance=0, lang="ru")
    await _seed_language(registry, _USER, "en")
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=4500,
        amount_crypto=5.0,
        currency="USDT",
        status="pending",
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update(WithdrawApprove(request_id=rid).pack(), user_id=_DEV)
    )

    # Row finalized — manual marker, no provider transfer id.
    row = await _row(registry, rid)
    assert row is not None
    assert row.status == "completed"
    assert row.processed_by == _DEV
    assert row.tx_hash == "manual"
    # Escrow was debited at create; approval must not move coins.
    assert await _balance(registry, _USER) == 0
    # User got a DM in English — their /lang choice, not the "ru" their
    # wallet row still carries (#1507).
    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _USER]
    assert dms, "approve must DM the user"
    assert dms[-1]["text"] == t("h_withdraw_dm_completed_manual", "en", request_id=rid)
    # Operator toast names the request too (dev has no wallet → RU default).
    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert answers
    assert answers[-1]["text"] == t(
        "h_withdraw_adm_completed_manual", "ru", request_id=rid, amount="5 USDT"
    )
    # #236: the operator is being told to move money by hand, so the
    # confirmation has to say how much — naming only the request id
    # sent them back to the panel to look it up. Asserted literally
    # rather than through ``t()`` alone: re-rendering the same template
    # would pass even if the amount were dropped from it.
    assert "5 USDT" in answers[-1]["text"]


@pytest.mark.asyncio
async def test_approve_already_processed_is_idempotent(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A second ✅ on the same row changes nothing and DMs no one — the
    pending-status guard resolves the double-click to ALREADY_PROCESSED."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed_wallet(registry, _USER, balance=0, lang="ru")
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=4500,
        currency="USDT",
        status="completed",  # already processed
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update(WithdrawApprove(request_id=rid).pack(), user_id=_DEV)
    )

    assert [e for e in sink if e["kind"] == "text" and e["chat_id"] == _USER] == []
    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert answers
    assert answers[-1]["text"] == t("h_withdraw_adm_already", "ru", request_id=rid)


@pytest.mark.asyncio
async def test_reject_refunds_user_and_marks_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """🚫 reject: the escrowed coins are credited back, the row flips to
    rejected, and the user is notified with the default (no-reason)
    wording."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    # Balance is 0 because the 4500 is escrowed; reject refunds it.
    await _seed_wallet(registry, _USER, balance=0, lang="ru")
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=4500,
        amount_crypto=5.0,
        currency="USDT",
        status="pending",
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update(WithdrawReject(request_id=rid).pack(), user_id=_DEV)
    )

    assert await _balance(registry, _USER) == 4500
    row = await _row(registry, rid)
    assert row is not None
    assert row.status == "rejected"
    assert row.processed_by == _DEV
    assert row.admin_note is None  # inline button carries no reason
    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _USER]
    assert dms, "reject must DM the refunded user"
    # No /lang row for this user, so the resolver falls back to the
    # operator's language — the documented last resort in
    # ``language_for_user`` when a third party has no preference on file.
    assert dms[-1]["text"] == t("h_withdraw_dm_rejected", "ru", request_id=rid, amount=4500)


@pytest.mark.asyncio
async def test_reject_command_with_reason(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/admin_withdrawals reject <id> <reason>``: refund + the reason
    lands in ``admin_note`` and (HTML-escaped) in the user's DM."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed_wallet(registry, _USER, balance=0, lang="ru")
    await _seed_language(registry, _USER, "en")
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=1200,
        currency="USDT",
        status="pending",
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_outgoing(bot)

    reason = "bad <details> & retry"
    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/admin_withdrawals reject {rid} {reason}",
            user_id=_DEV,
            chat_type="private",
        ),
    )

    assert await _balance(registry, _USER) == 1200
    row = await _row(registry, rid)
    assert row is not None
    assert row.status == "rejected"
    assert row.admin_note == reason  # stored raw — escaping is a render concern
    dms = [e for e in sink if e["chat_id"] == _USER]
    assert dms, "command reject must DM the refunded user"
    assert dms[-1]["text"] == t(
        "h_withdraw_dm_rejected_reason",
        "en",
        request_id=rid,
        amount=1200,
        reason="bad &lt;details&gt; &amp; retry",
    )
    # Operator got a reply in their chat naming the request.
    replies = [e for e in sink if e["chat_id"] == _DEV]
    assert replies
    assert replies[-1]["text"] == t("h_withdraw_adm_rejected", "ru", request_id=rid)


@pytest.mark.asyncio
async def test_command_bad_args_renders_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An unknown subcommand (or a non-numeric id) gets the usage hint
    and touches nothing."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=1200,
        currency="USDT",
        status="pending",
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_withdrawals reject not-a-number", user_id=_DEV, chat_type="private"
        ),
    )

    row = await _row(registry, rid)
    assert row is not None
    assert row.status == "pending"  # untouched
    assert len(sink) == 1
    assert sink[0]["text"] == t("h_withdraw_adm_usage", "ru")


@pytest.mark.asyncio
async def test_non_developer_click_is_dropped(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A non-dev clicking an action button changes nothing and DMs no one."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed_wallet(registry, _USER, balance=0, lang="ru")
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=4500,
        currency="USDT",
        status="pending",
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_callback_outgoing(bot)

    # Clicker id 12345 is not the developer.
    await dispatcher.feed_update(
        bot, make_callback_update(WithdrawReject(request_id=rid).pack(), user_id=12345)
    )

    row = await _row(registry, rid)
    assert row is not None
    assert row.status == "pending"  # untouched
    assert await _balance(registry, _USER) == 0  # no refund
    assert [e for e in sink if e["kind"] == "text"] == []  # no DM


@pytest.mark.asyncio
async def test_reject_click_dm_is_in_the_payees_language(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1507 on the 🚫 *button* path — a separate ``language_for_user``
    call site from the ``/admin_withdrawals reject`` command, so it can
    regress on its own.

    Both halves are pinned in one case on purpose: a "fix" that simply
    switched *both* the DM and the toast to the payee's language is the
    mirror-image bug, and a one-sided assertion would bless it.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed_wallet(registry, _USER, balance=0, lang="ru")
    await _seed_language(registry, _USER, "en")
    rid = await _seed_request(
        registry,
        user_id=_USER,
        amount_com=4500,
        amount_crypto=5.0,
        currency="USDT",
        status="pending",
        created_at="2026-01-01T00:00:00",
    )
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            WithdrawReject(request_id=rid).pack(),
            user_id=_DEV,
            language_code="ru",
        ),
    )

    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _USER]
    assert dms
    assert dms[-1]["text"] == t("h_withdraw_dm_rejected", "en", request_id=rid, amount=4500)

    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert answers
    assert answers[-1]["text"] == t("h_withdraw_adm_rejected", "ru", request_id=rid)
