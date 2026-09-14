"""``TtsService.synthesize`` — OpenAI TTS flow with wallet billing.

Mirrors the layering used by :mod:`tests.integration.services.test_transfer_service`:
the SDK boundary is mocked (``AsyncMock`` shaped as
``Mock(audio=Mock(speech=AsyncMock()))``); the EconomyRepo /
TransactionsRepo primitives are exercised against a real SQLite
schema so the debit row and balance update commit through real
SQL guards.

Outcome taxonomy is exhaustive — every :class:`TtsOutcome` member
has a test pinning its trigger condition + side-effect contract
(no charge on failure, no double-charge on success).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import TtsConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.tts_service import (
    TtsOutcome,
    TtsService,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _make_client(
    *, response: object | None = None, raises: Exception | None = None
) -> tuple[Any, AsyncMock]:
    """Construct a duck-typed OpenAI client + return its inner mock.

    The inner ``AsyncMock`` is returned so tests can assert on the
    call args (model, voice, input, timeout) and on call counts.
    """
    create_mock = AsyncMock()
    if raises is not None:
        create_mock.side_effect = raises
    else:
        create_mock.return_value = response

    client = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)))
    return client, create_mock


def _make_service(
    session: AsyncSession,
    client: Any | None,
    *,
    config: TtsConfig | None = None,
) -> TtsService:
    economy_repo = EconomyRepo(session)
    txs_repo = TransactionsRepo(session)
    economy_service = EconomyService(economy_repo, txs_repo)
    return TtsService(
        config or TtsConfig(),
        economy_service,
        client=client,
    )


async def _seed(session: AsyncSession, *, user_id: int, balance: int) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int | None:
    repo = EconomyRepo(session)
    wallet = await repo.get(user_id)
    return wallet.balance if wallet is not None else None


# ---------------------------------------------------------------------------
# coin_cost_for: char-count math
# ---------------------------------------------------------------------------


def test_coin_cost_rounds_up() -> None:
    """1 char * 0.001 = 0.001 → ceil → 1 (the min-1 floor + ceil).
    1000 chars * 0.001 = 1 → exact integer → 1.
    1001 chars * 0.001 = 1.001 → ceil → 2."""
    svc = TtsService(TtsConfig(), economy_service=None, client=None)  # type: ignore[arg-type]
    assert svc.coin_cost_for("a") == 1
    assert svc.coin_cost_for("a" * 1000) == 1
    assert svc.coin_cost_for("a" * 1001) == 2
    assert svc.coin_cost_for("") == 1  # min-1 floor


def test_coin_cost_with_custom_rate() -> None:
    """Operator drops the rate by 2x: 1000 chars now costs only 1
    coin still (1000 * 0.0005 = 0.5 → ceil → 1)."""
    cfg = TtsConfig(OPENAI_TTS_COIN_PER_CHAR=0.0005)
    svc = TtsService(cfg, economy_service=None, client=None)  # type: ignore[arg-type]
    assert svc.coin_cost_for("a" * 1000) == 1
    assert svc.coin_cost_for("a" * 2000) == 1
    assert svc.coin_cost_for("a" * 2001) == 2


# ---------------------------------------------------------------------------
# NOT_CONFIGURED — no client → no work
# ---------------------------------------------------------------------------


async def test_synthesize_without_client_returns_not_configured(
    session: AsyncSession,
) -> None:
    await _seed(session, user_id=1, balance=100)
    svc = _make_service(session, client=None)

    result = await svc.synthesize(user_id=1, text="hi", balance=100)
    assert result.outcome is TtsOutcome.NOT_CONFIGURED
    assert result.audio == b""
    # No ledger row.
    assert (await session.execute(select(Transaction))).first() is None
    assert await _balance(session, 1) == 100


# ---------------------------------------------------------------------------
# EMPTY_TEXT — whitespace-only payload short-circuits before upstream
# ---------------------------------------------------------------------------


async def test_synthesize_empty_text_short_circuits(session: AsyncSession) -> None:
    await _seed(session, user_id=1, balance=100)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"x"))
    svc = _make_service(session, client=client)

    result = await svc.synthesize(user_id=1, text="   ", balance=100)
    assert result.outcome is TtsOutcome.EMPTY_TEXT
    create_mock.assert_not_awaited()
    assert await _balance(session, 1) == 100


# ---------------------------------------------------------------------------
# TOO_LONG — text > max_chars rejects before pre-check
# ---------------------------------------------------------------------------


async def test_synthesize_too_long_refuses_before_upstream(
    session: AsyncSession,
) -> None:
    await _seed(session, user_id=1, balance=100)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"x"))
    cfg = TtsConfig(OPENAI_TTS_MAX_CHARS=5)
    svc = _make_service(session, client=client, config=cfg)

    result = await svc.synthesize(user_id=1, text="hello world", balance=100)
    assert result.outcome is TtsOutcome.TOO_LONG
    assert result.estimated_budget == 1  # 11 chars * 0.001 → ceil → 1
    create_mock.assert_not_awaited()
    assert await _balance(session, 1) == 100


# ---------------------------------------------------------------------------
# INSUFFICIENT_BALANCE — pre-check refusal, no upstream, no charge
# ---------------------------------------------------------------------------


async def test_synthesize_pre_check_insufficient_refuses(
    session: AsyncSession,
) -> None:
    await _seed(session, user_id=1, balance=0)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"x"))
    svc = _make_service(session, client=client)

    result = await svc.synthesize(user_id=1, text="hello", balance=0)
    assert result.outcome is TtsOutcome.INSUFFICIENT_BALANCE
    assert result.estimated_budget == 1
    create_mock.assert_not_awaited()
    assert (await session.execute(select(Transaction))).first() is None
    assert await _balance(session, 1) == 0


# ---------------------------------------------------------------------------
# SUCCESS — debit lands once, ledger row written, audio returned
# ---------------------------------------------------------------------------


async def test_synthesize_success_charges_and_returns_audio(
    session: AsyncSession,
) -> None:
    await _seed(session, user_id=1, balance=100)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"mp3-bytes"))
    svc = _make_service(session, client=client)

    result = await svc.synthesize(user_id=1, text="helloworld", balance=100)
    assert result.outcome is TtsOutcome.SUCCESS
    assert result.audio == b"mp3-bytes"
    assert result.coins_charged == 1
    assert result.balance_after == 99
    create_mock.assert_awaited_once()
    # Exactly one upstream call and exactly one ledger row.
    rows = (await session.execute(select(Transaction))).scalars().all()
    assert len(rows) == 1
    assert rows[0].type == "tts"
    assert rows[0].amount == 1
    assert rows[0].from_id == 1
    assert await _balance(session, 1) == 99


# ---------------------------------------------------------------------------
# SUCCESS with skip_billing — synthesis runs, no debit, no ledger row
# ---------------------------------------------------------------------------


async def test_synthesize_skip_billing_does_not_charge(
    session: AsyncSession,
) -> None:
    await _seed(session, user_id=1, balance=0)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"gift-mp3"))
    svc = _make_service(session, client=client)

    result = await svc.synthesize(user_id=1, text="hi", balance=0, skip_billing=True)
    assert result.outcome is TtsOutcome.SUCCESS
    assert result.audio == b"gift-mp3"
    assert result.coins_charged == 0
    create_mock.assert_awaited_once()
    # No ledger row, no balance change.
    assert (await session.execute(select(Transaction))).first() is None
    assert await _balance(session, 1) == 0


# ---------------------------------------------------------------------------
# UPSTREAM_ERROR — exception → no charge, no audio
# ---------------------------------------------------------------------------


async def test_synthesize_upstream_exception_returns_error_without_charge(
    session: AsyncSession,
) -> None:
    """M-P-3: upstream exception → pre-authorised debit refunded.

    The ledger holds a debit + refund pair (operator-side audit
    trail). The user-facing invariant is wallet-net-zero: balance
    after the failed call equals the balance before.
    """
    await _seed(session, user_id=1, balance=100)
    client, _ = _make_client(raises=RuntimeError("boom"))
    svc = _make_service(session, client=client)

    result = await svc.synthesize(user_id=1, text="hi", balance=100)
    assert result.outcome is TtsOutcome.UPSTREAM_ERROR
    assert result.audio == b""
    # Net wallet change: zero. The debit was refunded.
    assert await _balance(session, 1) == 100
    # Audit trail: one debit (type=tts) + one refund (type=tts_refund).
    rows = (await session.execute(select(Transaction))).scalars().all()
    types = sorted(r.type for r in rows)
    assert types == ["tts", "tts_refund"]


async def test_synthesize_non_bytes_response_returns_error(
    session: AsyncSession,
) -> None:
    """A response without a ``.content: bytes`` attribute is treated
    as UPSTREAM_ERROR — not an AttributeError that 500s the webhook
    by tearing down the handler's transaction. M-P-3: pre-debit is
    refunded so the balance is unchanged."""
    await _seed(session, user_id=1, balance=100)
    client, _ = _make_client(response=SimpleNamespace(content="not-bytes"))
    svc = _make_service(session, client=client)

    result = await svc.synthesize(user_id=1, text="hi", balance=100)
    assert result.outcome is TtsOutcome.UPSTREAM_ERROR
    assert await _balance(session, 1) == 100
    rows = (await session.execute(select(Transaction))).scalars().all()
    types = sorted(r.type for r in rows)
    assert types == ["tts", "tts_refund"]


async def test_synthesize_oversize_audio_refunds_and_refuses(
    session: AsyncSession,
) -> None:
    """M-P-3: audio bytes exceed ``max_audio_bytes`` cap → refunded.

    Spec: pre-authorised debit must be reversed and the user sees
    AUDIO_TOO_LARGE; the upload to Telegram never happens.
    """
    await _seed(session, user_id=1, balance=100)
    huge = b"x" * (5 * 1024)  # 5KB, well over our test cap
    client, _ = _make_client(response=SimpleNamespace(content=huge))
    tts_config = TtsConfig(OPENAI_TTS_MAX_AUDIO_BYTES=1024)
    svc = _make_service(session, client=client, config=tts_config)

    result = await svc.synthesize(user_id=1, text="hi", balance=100)
    assert result.outcome is TtsOutcome.AUDIO_TOO_LARGE
    assert result.audio == b""
    # Balance net-zero.
    assert await _balance(session, 1) == 100
    rows = (await session.execute(select(Transaction))).scalars().all()
    types = sorted(r.type for r in rows)
    assert types == ["tts", "tts_refund"]


# ---------------------------------------------------------------------------
# No double-charge on a single invocation (defensive — the service's
# debit call site is one line, but pinning the count keeps a future
# refactor honest)
# ---------------------------------------------------------------------------


async def test_synthesize_single_invocation_debits_exactly_once(
    session: AsyncSession,
) -> None:
    await _seed(session, user_id=1, balance=100)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"x"))
    svc = _make_service(session, client=client)

    # Two long-ish chars → 1 coin (rounded up from 0.002).
    await svc.synthesize(user_id=1, text="hi", balance=100)
    create_mock.assert_awaited_once()
    rows = (await session.execute(select(Transaction))).scalars().all()
    assert len(rows) == 1
    assert rows[0].amount == 1
    assert await _balance(session, 1) == 99


# ---------------------------------------------------------------------------
# call args propagation — voice / model / input flow through unchanged
# ---------------------------------------------------------------------------


async def test_synthesize_passes_config_to_sdk(session: AsyncSession) -> None:
    """The service passes ``model``, ``voice``, ``input``, ``timeout``
    through to ``client.audio.speech.create``. Pinning this keeps a
    future "let's hard-code the voice" refactor visible as a test
    failure rather than silent operator-confusion."""
    await _seed(session, user_id=1, balance=100)
    client, create_mock = _make_client(response=SimpleNamespace(content=b"x"))
    cfg = TtsConfig(
        OPENAI_TTS_VOICE="alloy",
        OPENAI_TTS_MODEL="tts-1-hd",
        OPENAI_TTS_TIMEOUT_SECONDS=42.0,
    )
    svc = _make_service(session, client=client, config=cfg)

    await svc.synthesize(user_id=1, text="hello", balance=100)
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    assert kwargs["voice"] == "alloy"
    assert kwargs["model"] == "tts-1-hd"
    assert kwargs["input"] == "hello"
    assert kwargs["timeout"] == 42.0
