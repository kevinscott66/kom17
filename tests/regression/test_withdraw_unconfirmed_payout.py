"""Regression guard: an unknown payout outcome must not become a refund.

``WithdrawService.approve`` has two ways to end badly, and they are not
neighbours:

* the provider **refused** — the app wallet is short, or the API answered
  ``ok:false``. No money moved. The request goes back to ``pending`` so
  an admin can retry it or reject it.
* the provider **didn't answer** — a read timeout, a 502, a body that
  isn't JSON. The transfer may have executed.

Before T-020 R14 both raised a bare ``CryptoPayError`` and both released
the lease, which turned the second one into a way to be paid twice::

    approve   pays USDT, reply lost   → lease released → pending
    admin     sees it back in the queue, taps «Отклонить»
    reject    refunds every escrowed coin

The user ends up holding the crypto *and* the coins, and the log says
only "provider error". :class:`CryptoPayUnconfirmed` splits the two, and
this file keeps them split from both ends: which HTTP outcomes the client
calls unknown, and what the service does when it hears that.

Scope note: ``approve`` is the auto-payout path and is deliberately not
wired to the admin panel yet (payout doctrine v1 is manual — see
``handlers/admin/withdrawals.py``). The guard is written now precisely
because the wiring is what would make this reachable, and a money bug is
much cheaper to hold shut than to notice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, WithdrawalRequest
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.withdrawals_repo import (
    PROCESSING_STATUS,
    WithdrawalsRepo,
)
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments.crypto_client import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayInsufficientFunds,
    CryptoPayUnconfirmed,
    TransferResult,
)
from telegram_invite_bot.services.withdraw_service import (
    ApproveOutcome,
    CreateOutcome,
    RejectOutcome,
    WithdrawService,
)

_USER = 4242
_COINS_PER_USDT = 900.0
_MIN = 4500
_MAX = 90000
_BALANCE = 10_000
_AMOUNT = 4500


# ---------------------------------------------------------------------------
# The client: which HTTP outcomes are "unknown"
# ---------------------------------------------------------------------------


def _client(handler: object) -> CryptoPayClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return CryptoPayClient("tok", client=httpx.AsyncClient(transport=transport))


def _ok(payload: object) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def _status(code: int) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"ok": False})

    return handler


def _raises(exc: Exception) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


async def _transfer(client: CryptoPayClient) -> TransferResult:
    return await client.transfer(user_id=_USER, asset="USDT", amount="5.0", spend_id="wd_1")


#: Outcomes where the request may have executed and we cannot tell.
_UNKNOWN: dict[str, object] = {
    "the reply never arrived": _raises(httpx.ReadTimeout("timed out")),
    "the connection dropped mid-flight": _raises(httpx.RemoteProtocolError("reset")),
    # A gateway 5xx says nothing about whether the backend behind it
    # committed the transfer before failing to report back.
    "a gateway 502": _status(502),
    "the provider is briefly down (503)": _status(503),
    # 200 with an unreadable body is what a captive portal or an
    # interstitial proxy looks like — possibly in front of a request that
    # went through.
    "a 200 that isn't JSON": _ok(None),
    "a 200 carrying a list instead of an object": _ok([1, 2, 3]),
    # The loudest one: the provider itself said ``ok``.
    "ok:true with the receipt missing": _ok({"ok": True}),
    "ok:true with a non-object result": _ok({"ok": True, "result": "done"}),
}

#: Outcomes decided before the provider touched a wallet.
_REFUSALS: dict[str, object] = {
    "a malformed request (400)": _status(400),
    "a bad token (401)": _status(401),
    "a rate limit (429)": _status(429),
    "an explicit api error": _ok({"ok": False, "error": {"name": "METHOD_DISABLED"}}),
}


@pytest.mark.parametrize(("case", "handler"), list(_UNKNOWN.items()))
async def test_an_unanswered_transfer_is_unconfirmed(case: str, handler: object) -> None:
    with pytest.raises(CryptoPayUnconfirmed):
        await _transfer(_client(handler))


@pytest.mark.parametrize(("case", "handler"), list(_REFUSALS.items()))
async def test_a_refused_transfer_stays_a_plain_error(case: str, handler: object) -> None:
    """4xx and ``ok:false`` must NOT be unconfirmed.

    Calling them unknown would leave the request in a status no admin
    surface lists, forever — a bad token never stops being a bad token,
    so the retry that clears an unconfirmed row would never clear this
    one. Trading a money bug for a stuck-queue bug is not a fix.
    """
    with pytest.raises(CryptoPayError) as excinfo:
        await _transfer(_client(handler))
    assert not isinstance(excinfo.value, CryptoPayUnconfirmed), (
        f"{case} was classified as an unknown outcome — it is a refusal"
    )


async def test_a_short_app_wallet_is_still_its_own_refusal() -> None:
    """The insufficient-funds branch must not be swallowed by the split."""
    handler = _ok({"ok": False, "error": {"name": "NOT_ENOUGH_COINS_TO_TRANSFER"}})
    with pytest.raises(CryptoPayInsufficientFunds) as excinfo:
        await _transfer(_client(handler))
    assert not isinstance(excinfo.value, CryptoPayUnconfirmed)


async def test_a_good_response_still_returns_a_receipt() -> None:
    """Non-vacuity: the transport plumbing above can actually succeed."""
    handler = _ok(
        {
            "ok": True,
            "result": {
                "transfer_id": 99,
                "spend_id": "wd_1",
                "asset": "USDT",
                "amount": "5.0",
            },
        }
    )
    result = await _transfer(_client(handler))
    assert result.transfer_id == 99


def test_unconfirmed_is_catchable_as_a_provider_error() -> None:
    """Callers that only care "it failed" must keep working unchanged.

    ``crypto_invoices`` catches :class:`CryptoPayError` around
    ``createInvoice``; the new class has to stay inside that net, or the
    split would turn a handled top-up failure into an unhandled crash.
    """
    assert issubclass(CryptoPayUnconfirmed, CryptoPayError)


# ---------------------------------------------------------------------------
# The service: what an unknown outcome does to the request
# ---------------------------------------------------------------------------


class _FakeClient:
    """Transfer stub. ``mode`` picks how the provider "answers"."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[str] = []

    async def transfer(
        self,
        *,
        user_id: int,
        asset: str,
        amount: str,
        spend_id: str,
        comment: str | None = None,
    ) -> TransferResult:
        self.calls.append(spend_id)
        if self.mode == "unknown":
            raise CryptoPayUnconfirmed("transfer transport error: timed out")
        if self.mode == "refused":
            raise CryptoPayError("transfer: api error METHOD_DISABLED")
        return TransferResult(transfer_id=7, spend_id=spend_id, asset=asset, amount=amount)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as opened:
        yield opened
    await engine.dispose()


def _service(session: AsyncSession) -> WithdrawService:
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))
    return WithdrawService(
        session=session,
        economy=economy,
        withdrawals=WithdrawalsRepo(session),
        coins_per_usdt=_COINS_PER_USDT,
        min_coins=_MIN,
        max_coins=_MAX,
        asset="USDT",
        daily_limit_coins=1_000_000,
        monthly_limit_coins=10_000_000,
    )


async def _row_status(session: AsyncSession, request_id: int) -> str | None:
    row = await session.execute(
        select(WithdrawalRequest.status).where(WithdrawalRequest.id == request_id)
    )
    return row.scalar_one_or_none()


async def _balance(session: AsyncSession) -> int | None:
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == _USER))
    value = row.scalar_one_or_none()
    return int(value) if value is not None else None


async def _escrowed(session: AsyncSession) -> tuple[WithdrawService, int]:
    """Seed a funded user and put one request into escrow."""
    session.add(EconomyUser(user_id=_USER, balance=_BALANCE, language="ru"))
    await session.commit()
    service = _service(session)
    created = await service.create(user_id=_USER, amount_com=_AMOUNT)
    assert created.outcome is CreateOutcome.OK
    assert created.request_id is not None
    assert await _balance(session) == _BALANCE - _AMOUNT
    return service, created.request_id


async def test_an_unknown_payout_keeps_the_lease(session: AsyncSession) -> None:
    """The row must stay ``processing`` — that status is the protection.

    ``pending`` is the rejectable status. Handing the row back there is
    what lets an admin refund coins for a transfer that may already have
    landed, so the lease has to survive an outcome we can't read.
    """
    service, request_id = await _escrowed(session)
    result = await service.approve(
        request_id=request_id,
        admin_id=1,
        client=_FakeClient("unknown"),  # type: ignore[arg-type]
    )
    assert result.outcome is ApproveOutcome.PAYOUT_UNCONFIRMED
    assert await _row_status(session, request_id) == PROCESSING_STATUS
    # Escrow untouched in both directions: not refunded, not re-debited.
    assert await _balance(session) == _BALANCE - _AMOUNT


async def test_a_refused_payout_still_releases_the_lease(session: AsyncSession) -> None:
    """The other half of the split, so the fix can't become "never release".

    A definitive refusal must keep returning the request to the queue —
    otherwise every failed payout strands a row an admin can't see.
    """
    service, request_id = await _escrowed(session)
    result = await service.approve(
        request_id=request_id,
        admin_id=1,
        client=_FakeClient("refused"),  # type: ignore[arg-type]
    )
    assert result.outcome is ApproveOutcome.PROVIDER_ERROR
    assert await _row_status(session, request_id) == "pending"


async def test_an_unknown_payout_cannot_then_be_refunded(session: AsyncSession) -> None:
    """The exploit, run end to end: approve → timeout → reject.

    This is the assertion the whole file exists for. If the lease were
    released, ``reject`` would find a ``pending`` row and credit every
    escrowed coin back on top of a payout that may already have landed.
    """
    service, request_id = await _escrowed(session)
    await service.approve(
        request_id=request_id,
        admin_id=1,
        client=_FakeClient("unknown"),  # type: ignore[arg-type]
    )

    rejected = await service.reject(request_id=request_id, admin_id=2)
    assert rejected.outcome is RejectOutcome.ALREADY_PROCESSED
    assert await _row_status(session, request_id) == PROCESSING_STATUS
    assert await _balance(session) == _BALANCE - _AMOUNT, "the escrow was refunded"


async def test_an_unknown_payout_cannot_be_hand_completed(session: AsyncSession) -> None:
    """``approve_manual`` guards on ``pending`` too — it must stay shut.

    Otherwise the manual panel becomes the way around the lease: mark it
    paid by hand, and the auto-payout that may already have gone out is
    never reconciled against anything.
    """
    service, request_id = await _escrowed(session)
    await service.approve(
        request_id=request_id,
        admin_id=1,
        client=_FakeClient("unknown"),  # type: ignore[arg-type]
    )
    manual = await service.approve_manual(request_id=request_id, admin_id=2)
    assert manual.outcome is ApproveOutcome.ALREADY_PROCESSED
    assert await _row_status(session, request_id) == PROCESSING_STATUS


async def test_the_leased_request_is_still_re_approvable(session: AsyncSession) -> None:
    """Keeping the lease must not be a dead end.

    ``claim_processing`` matches ``processing`` as well as ``pending``
    exactly so a re-drive works, and ``spend_id`` makes the second
    provider call free of a double payout. That pair is what makes
    "leave it leased" a safe default rather than a stuck row.
    """
    service, request_id = await _escrowed(session)
    await service.approve(
        request_id=request_id,
        admin_id=1,
        client=_FakeClient("unknown"),  # type: ignore[arg-type]
    )

    good = _FakeClient("ok")
    retried = await service.approve(
        request_id=request_id,
        admin_id=1,
        client=good,  # type: ignore[arg-type]
    )
    assert retried.outcome is ApproveOutcome.COMPLETED
    assert await _row_status(session, request_id) == "completed"
    # Same idempotency key both times — the provider pays once.
    assert good.calls == [f"wd_{request_id}"]
    assert await _balance(session) == _BALANCE - _AMOUNT


async def test_the_unknown_branch_is_ordered_before_the_generic_one(
    session: AsyncSession,
) -> None:
    """Ordering is load-bearing, and it is invisible at the call site.

    :class:`CryptoPayUnconfirmed` is a ``CryptoPayError`` subclass, so
    swapping the two ``except`` clauses silently restores the original
    bug — the row would go back to ``pending`` and every assertion about
    the provider outcome above would still read ``PROVIDER_ERROR``. Pin
    the outcome enum member so that swap can't pass.
    """
    service, request_id = await _escrowed(session)
    result = await service.approve(
        request_id=request_id,
        admin_id=1,
        client=_FakeClient("unknown"),  # type: ignore[arg-type]
    )
    assert result.outcome is not ApproveOutcome.PROVIDER_ERROR
    assert result.outcome is ApproveOutcome.PAYOUT_UNCONFIRMED


def test_every_approve_outcome_has_copy_of_its_own() -> None:
    """The other end of the same bug: what the admin is *told*.

    The panel still calls ``approve_manual``, so none of the provider
    outcomes can reach a screen today — which is exactly why the map is
    easy to leave incomplete until the wiring makes it reachable. An
    unmapped outcome falls through to «Не удалось обработать заявку»,
    and for PAYOUT_UNCONFIRMED that is a lie in the expensive direction:
    the transfer may have landed, the row was left leased on purpose,
    and an admin who reads "failed" pays the user a second time by hand.
    """
    from telegram_invite_bot.handlers.admin.withdrawals import _APPROVE_TOASTS

    unmapped = {
        outcome
        for outcome in ApproveOutcome
        if outcome is not ApproveOutcome.COMPLETED and outcome not in _APPROVE_TOASTS
    }
    assert not unmapped, f"no admin toast for: {sorted(o.name for o in unmapped)}"


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_unknown_outcome_does_not_read_as_nothing_happened(lang: str) -> None:
    """Copy, not just a key.

    ``t()`` returns the key itself when the locale has no entry, so a
    map filled with plausible-looking key names would still pass the
    test above while showing the admin ``h_withdraw_adm_...`` on screen.
    """
    from telegram_invite_bot.handlers.admin.withdrawals import _APPROVE_TOASTS

    key = _APPROVE_TOASTS[ApproveOutcome.PAYOUT_UNCONFIRMED]
    text = t(key, lang, request_id=42)
    assert text != key, "the locale has no entry for this key"
    assert text != t("h_withdraw_adm_failed", lang, request_id=42)
    # Rendered into a callback answer, which Telegram truncates at 200.
    assert len(text) <= 200
