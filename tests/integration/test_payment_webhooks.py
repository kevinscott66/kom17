"""E2E tests for ported payment webhooks (T-025).

Drives the live FastAPI router against in-memory SQLite engines:
each request goes through the real adapter (signature verification +
parse), the real PaymentsService (idempotency + credit + processed
row), and produces the real EconomyService side effects (wallet row
update + ledger row).

Adapters that pull third-party SDKs (yookassa, stripe) get stubbed
via ``sys.modules`` injection — we never make real HTTP calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dishka import make_async_container
from fastapi.testclient import TestClient
from loguru import logger
from pydantic import SecretStr
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from telegram_invite_bot.app import Application
from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    PaymentsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import EngineRegistry, build_registry
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    ProcessedWebhook,
    Transaction,
)
from telegram_invite_bot.db.models.user_settings import UserSetting
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.processed_webhooks_repo import (
    CreditRecord,
    ProcessedWebhooksRepo,
)
from telegram_invite_bot.services.currency_service import CurrencyService
from telegram_invite_bot.services.payments.rollypay import REVERSAL_EVENTS
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from telegram_invite_bot.utils.http_body import PAYMENT_WEBHOOK_MAX_BYTES
from telegram_invite_bot.webhook.metrics import (
    PAYMENT_CREDIT_FAILURES,
    PAYMENT_DM_FAILURES,
    PAYMENT_REVERSALS,
    PAYMENT_UNCREDITED,
)
from telegram_invite_bot.webhook.server import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CRYPTO_TOKEN = "test-crypto-token"
_STRIPE_SECRET = "whsec_test"
_YOOKASSA_SHOP = "shop-1"
_YOOKASSA_SECRET = "yoo-secret"
_ROLLYPAY_SECRET = "rolly-signing-secret"


def _settings(
    tmp_path: Path,
    *,
    crypto: bool = True,
    yookassa: bool = True,
    stripe: bool = True,
    rollypay: bool = True,
) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(
            WEBHOOK_PATH="/webhook",
            WEBHOOK_SECRET_TOKEN=SecretStr("dummy"),
        ),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
        payments=PaymentsConfig(
            CRYPTO_PAY_TOKEN=SecretStr(_CRYPTO_TOKEN) if crypto else None,
            YOOKASSA_SHOP_ID=_YOOKASSA_SHOP if yookassa else None,
            YOOKASSA_SECRET_KEY=SecretStr(_YOOKASSA_SECRET) if yookassa else None,
            STRIPE_WEBHOOK_SECRET=SecretStr(_STRIPE_SECRET) if stripe else None,
            ROLLYPAY_SIGNING_SECRET=(SecretStr(_ROLLYPAY_SECRET) if rollypay else None),
        ),
    )


async def _build_application(settings: Settings) -> Application:
    engines = build_registry(settings)
    # Schemas: economy (wallet + ledger + processed_webhooks) and
    # users (user_settings for language lookup).
    for base, db in (
        (EconomyBase, DBName.ECONOMY),
        (UsersBase, DBName.USERS),
    ):
        engine = engines.engine(db)
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    return Application(
        container=make_async_container(),
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        engines=engines,
    )


@pytest.fixture
async def application(tmp_path: Path) -> AsyncIterator[Application]:
    app = await _build_application(_settings(tmp_path))
    try:
        yield app
    finally:
        await app.close()


async def _seed_wallet(
    application: Application, user_id: int, balance: int = 1000, lang: str = "ru"
) -> None:
    async with application.engines.session(DBName.ECONOMY)() as s:
        s.add(EconomyUser(user_id=user_id, balance=balance, language=lang))
        await s.commit()
    async with application.engines.session(DBName.USERS)() as s:
        s.add(User(user_id=user_id))
        await s.flush()
        s.add(UserSetting(user_id=user_id, language=lang))
        await s.commit()


@pytest.fixture(autouse=True)
def _offline_fx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the R11 FX lookup off the wire for every test in this module.

    ``create_app`` wires a real :class:`CurrencyService` into the
    payments router so a YooKassa credit can be priced off the live
    USD/RUB fix. Left alone it would try exchangerate-api.com on the
    first webhook of each test. Stubbing ``_fetch`` to ``None`` drops
    the service onto its own offline table, whose re-anchored RUB leg
    is exactly :data:`FALLBACK_USD_TO_RUB` — so every pre-R11
    expectation in this file (499 RUB → 4990 coins) still holds, with
    no network and no timeout. Tests that care about a *different* fix
    pin it explicitly; see the R11 block at the bottom.
    """
    monkeypatch.setattr(CurrencyService, "_fetch", _async_none)


async def _async_none(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.fixture
def client(application: Application) -> Iterator[TestClient]:
    fastapi_app = create_app(application)
    # Disable outbound Bot.send_message — we don't want to hit
    # Telegram during tests. The DM is best-effort and its absence
    # doesn't affect the credit path.
    fastapi_app.state.application.bot.send_message = _async_noop  # type: ignore[method-assign]
    with TestClient(fastapi_app) as c:
        yield c


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _wallet_balance(application: Application, user_id: int) -> int | None:
    async with application.engines.session(DBName.ECONOMY)() as s:
        row = await s.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
        result = row.scalar_one_or_none()
        return int(result) if result is not None else None


async def _ledger_count(application: Application, user_id: int) -> int:
    async with application.engines.session(DBName.ECONOMY)() as s:
        row = await s.execute(
            select(func.count()).select_from(Transaction).where(Transaction.to_id == user_id)
        )
        return int(row.scalar_one())


async def _processed_count(application: Application, provider: str) -> int:
    async with application.engines.session(DBName.ECONOMY)() as s:
        row = await s.execute(
            select(func.count())
            .select_from(ProcessedWebhook)
            .where(ProcessedWebhook.provider == provider)
        )
        return int(row.scalar_one())


# ---------------------------------------------------------------------------
# Crypto Pay
# ---------------------------------------------------------------------------


def _crypto_sign(body: bytes, token: str = _CRYPTO_TOKEN) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _crypto_payload(amount: str = "1.0", invoice_id: str = "INV-1") -> dict[str, Any]:
    return {
        "update_type": "invoice_paid",
        "payload": {
            "invoice_id": invoice_id,
            "payload": "777",  # legacy stuffs user_id here
            "amount": amount,
            "paid_usd_rate": "1.0",
            "asset": "USDT",
        },
    }


async def test_crypto_valid_signature_credits_wallet(
    application: Application, client: TestClient
) -> None:
    await _seed_wallet(application, 777, balance=100)
    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # $1.0 * 900 coins/USD = 900 credited.
    assert await _wallet_balance(application, 777) == 100 + 900
    assert await _ledger_count(application, 777) == 1
    assert await _processed_count(application, "crypto") == 1


async def test_crypto_duplicate_delivery_does_not_double_credit(
    application: Application, client: TestClient
) -> None:
    await _seed_wallet(application, 777, balance=100)
    body = json.dumps(_crypto_payload()).encode()
    headers = {
        "content-type": "application/json",
        "crypto-pay-api-signature": _crypto_sign(body),
    }
    r1 = client.post("/crypto-webhook", content=body, headers=headers)
    r2 = client.post("/crypto-webhook", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Balance only moved once despite two deliveries.
    assert await _wallet_balance(application, 777) == 100 + 900
    assert await _processed_count(application, "crypto") == 1


async def test_crypto_bad_signature_rejected_no_credit(
    application: Application, client: TestClient
) -> None:
    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": "deadbeef" * 8,
        },
    )
    assert resp.status_code == 403
    assert resp.json() == {"ok": False, "error": "invalid_signature"}


async def test_crypto_missing_config_returns_503(tmp_path: Path) -> None:
    """An unconfigured route still says so — to a plausible caller.

    The header has to be well-shaped for this test to mean anything
    since #1470: the shape gate now refuses a misshapen signature with
    403 before the token is ever looked up, so the one-byte header this
    used to send would exercise that gate instead of the branch under
    test. A 64-hex string is what Crypto Pay actually sends, and it is
    the only kind of caller for whom "not_configured" is the honest
    answer.
    """
    application = await _build_application(_settings(tmp_path, crypto=False))
    try:
        fastapi_app = create_app(application)
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/crypto-webhook",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": "a1" * 32,
                },
            )
            assert resp.status_code == 503
            assert resp.json()["error"] == "not_configured"
    finally:
        await application.close()


async def _seed_runtime_token(application: Application, token: str) -> None:
    """Write the ``CRYPTO_PAY_TOKEN`` runtime override (T-027) — the
    same row the in-bot ``/set_crypto_token`` panel upserts.
    """
    from telegram_invite_bot.repositories.runtime_secrets_repo import RuntimeSecretsRepo
    from telegram_invite_bot.services.payments.secret_resolver import CRYPTO_PAY_TOKEN_KEY

    async with application.engines.session(DBName.ECONOMY)() as s:
        await RuntimeSecretsRepo(s).upsert(CRYPTO_PAY_TOKEN_KEY, token, updated_by=1)
        await s.commit()


async def test_crypto_runtime_override_token_wins_over_env(
    application: Application, client: TestClient
) -> None:
    """T-027: a token set via the in-bot panel (``runtime_secrets``)
    feeds the inbound signature check — overriding the ``.env`` value.
    A body signed with the OVERRIDE token verifies and credits; the
    env token is no longer the one being checked.
    """
    override = "override-crypto-token"
    await _seed_runtime_token(application, override)
    await _seed_wallet(application, 777, balance=100)
    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            # Signed with the OVERRIDE token, not the env _CRYPTO_TOKEN.
            "crypto-pay-api-signature": _crypto_sign(body, token=override),
        },
    )
    assert resp.status_code == 200
    assert await _wallet_balance(application, 777) == 100 + 900


async def test_crypto_env_token_rejected_once_override_set(
    application: Application, client: TestClient
) -> None:
    """The flip side: once an override is set, a body signed with the
    OLD env token must fail signature verification — proving the
    resolver, not the env var, is the source of truth at call time.
    """
    await _seed_runtime_token(application, "override-crypto-token")
    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            # Old env token — no longer the effective secret.
            "crypto-pay-api-signature": _crypto_sign(body, token=_CRYPTO_TOKEN),
        },
    )
    assert resp.status_code == 403


async def test_crypto_runtime_override_enables_when_env_unset(tmp_path: Path) -> None:
    """The core dev-panel use case: no ``CRYPTO_PAY_TOKEN`` in ``.env``,
    but a developer sets it at runtime via the panel — the webhook
    must flip from 503 to live WITHOUT a redeploy.
    """
    application = await _build_application(_settings(tmp_path, crypto=False))
    try:
        override = "panel-set-token"
        await _seed_runtime_token(application, override)
        await _seed_wallet(application, 777, balance=100)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _async_noop  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            body = json.dumps(_crypto_payload()).encode()
            resp = c.post(
                "/crypto-webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": _crypto_sign(body, token=override),
                },
            )
            assert resp.status_code == 200
            assert await _wallet_balance(application, 777) == 100 + 900
    finally:
        await application.close()


async def test_crypto_unknown_user_is_seeded_and_credited(
    application: Application, client: TestClient
) -> None:
    """#770: a verified crypto payment seeds the wallet it needs.

    This case used to assert the opposite — 200, no wallet, no
    idempotency row, on the theory that "provider would retry, finds
    same state". The retry never fixed anything: nothing about a
    redelivery creates the missing row, so the payment stayed banked
    and uncredited until somebody read the alert.
    """
    # No wallet seeded for user 777.
    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )
    assert resp.status_code == 200
    # 100 is the welcome credit on a fresh row (``EconomyUser`` docstring);
    # the seeded-wallet sibling above ends at 100 + 900 too, which is
    # the point — an unknown payer is not a second-class one.
    assert await _wallet_balance(application, 777) == 100 + 900
    assert await _processed_count(application, "crypto") == 1


async def test_crypto_non_invoice_paid_event_acknowledged_no_credit(
    application: Application, client: TestClient
) -> None:
    await _seed_wallet(application, 777, balance=100)
    payload = _crypto_payload()
    payload["update_type"] = "invoice_expired"
    body = json.dumps(payload).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )
    assert resp.status_code == 200
    assert await _wallet_balance(application, 777) == 100


# ---------------------------------------------------------------------------
# YooKassa
# ---------------------------------------------------------------------------


def _install_fake_yookassa(
    monkeypatch: pytest.MonkeyPatch,
    *,
    succeeded: bool,
    rub: str = "499.00",
    currency: str = "RUB",
    user_id: str | None = "42",
    coins: str = "4990",
    calls: list[str] | None = None,
    raises: Exception | None = None,
) -> None:
    """Stand in for ``Payment.find_one``.

    Since T-020 R11-b this object — not the request body — is what the
    credit is derived from, so it carries the amount and metadata the
    real SDK returns. Tests that want the body and the authoritative
    record to disagree pass different values here than they post.

    Pass ``calls`` to record every ``find_one`` argument: reverify is
    a blocking merchant-API round-trip, so #228 needs to assert on
    whether it happened at all, not only on its result.

    Pass ``raises`` for the round-trip that never completes — no
    network, a 5xx from the merchant API, rejected credentials. That
    is a different outcome from ``succeeded=False`` and the route owes
    it a different answer; see
    :func:`test_yookassa_unreachable_merchant_api_asks_for_redelivery`.
    """
    fake = types.ModuleType("yookassa")

    class _Configuration:
        account_id: str = ""
        secret_key: str = ""

    amount_obj = types.SimpleNamespace(value=rub, currency=currency)
    meta = {"coins": coins} | ({} if user_id is None else {"user_id": user_id})

    class _Payment:
        status = "succeeded" if succeeded else "pending"
        amount = amount_obj
        metadata = meta

        @classmethod
        def find_one(cls, payment_id: str) -> _Payment:
            if calls is not None:
                calls.append(payment_id)
            if raises is not None:
                raise raises
            return cls()

    fake.Configuration = _Configuration  # type: ignore[attr-defined]
    fake.Payment = _Payment  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yookassa", fake)


def _yookassa_payload(
    payment_id: str = "PAY-1",
    *,
    rub: str = "499.00",
    coins: str = "4990",  # 499 RUB * 10 coins/RUB == server-derived
) -> dict[str, Any]:
    return {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "amount": {"value": rub, "currency": "RUB"},
            "metadata": {"user_id": "42", "coins": coins},
        },
    }


async def test_yookassa_valid_credits_wallet(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 200
    assert resp.content == b""
    assert await _wallet_balance(application, 42) == 100 + 4990
    assert await _processed_count(application, "yookassa") == 1


async def test_yookassa_duplicate_delivery_does_not_double_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    r1 = client.post("/yookassa-webhook", json=_yookassa_payload())
    r2 = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert await _wallet_balance(application, 42) == 100 + 4990
    assert await _processed_count(application, "yookassa") == 1


@pytest.mark.parametrize(
    "forged_object",
    [
        pytest.param({"id": "PAY-FORGED"}, id="no-metadata-at-all"),
        pytest.param({"id": "PAY-FORGED", "metadata": {}}, id="empty-metadata"),
        pytest.param({"id": "PAY-FORGED", "metadata": {"user_id": "42"}}, id="no-coins"),
        pytest.param({"id": "PAY-FORGED", "metadata": {"coins": "4990"}}, id="no-user-id"),
        pytest.param(
            {"id": "PAY-FORGED", "metadata": {"user_id": "0", "coins": "0"}},
            id="zeroed",
        ),
        pytest.param({"id": "PAY-FORGED", "metadata": "not-a-mapping"}, id="metadata-not-a-map"),
        pytest.param(
            {"id": "PAY-FORGED", "metadata": {"user_id": "x", "coins": "y"}},
            id="unparsable",
        ),
    ],
)
async def test_yookassa_body_without_metadata_never_reaches_the_merchant_api(
    application: Application,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    forged_object: dict[str, Any],
) -> None:
    """#228: an anonymous POST must not cost us a merchant-API call.

    ``Payment.find_one`` is a blocking HTTPS round-trip issued from
    inside the async route with no ``to_thread``, so every forged body
    that reaches it parks the single event loop for the duration.
    Legacy refused bodies carrying no ``metadata.user_id``/``coins``
    *before* reverifying (webhook_server.py:163); the port dropped
    that precondition silently, so ``{"event": "payment.succeeded",
    "object": {"id": "X"}}`` — a body anyone can post — was enough to
    make the call.

    Every shape below is cheap for an attacker to send and impossible
    for a genuine notification to be, and each one exercises a
    different arm of the precondition: the missing/!mapping shapes hit
    the type check, the zeroed and partial ones hit the value check,
    and the unparsable one hits the ``int()`` guard. Asserting only
    the first would leave the others free to regress.

    This does not make the body trustworthy — it asserts only that a
    body which cannot possibly be one of ours is dropped for free.
    """
    calls: list[str] = []
    _install_fake_yookassa(monkeypatch, succeeded=True, calls=calls)

    resp = client.post(
        "/yookassa-webhook",
        json={"event": "payment.succeeded", "object": forged_object},
    )

    assert resp.status_code == 200
    assert calls == []
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_genuine_body_still_reverifies_and_credits(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #228 — the guard must not shut the door on
    the real thing.

    Every YooKassa payment this bot can be notified about was created
    with ``metadata={"user_id": ..., "coins": ...}`` (bot.py:18435 and
    bot.py:18580; the port has no YooKassa checkout of its own), so a
    genuine notification always clears the precondition, reaches
    ``find_one`` and credits from the reverified record. Without this
    the test above would also pass with the whole route removed.
    """
    await _seed_wallet(application, 42, balance=0)
    calls: list[str] = []
    _install_fake_yookassa(monkeypatch, succeeded=True, calls=calls)

    resp = client.post("/yookassa-webhook", json=_yookassa_payload())

    assert resp.status_code == 200
    assert calls == ["PAY-1"]
    assert await _wallet_balance(application, 42) == 4990


async def test_yookassa_missing_config_returns_503(tmp_path: Path) -> None:
    application = await _build_application(_settings(tmp_path, yookassa=False))
    try:
        fastapi_app = create_app(application)
        with TestClient(fastapi_app) as c:
            resp = c.post("/yookassa-webhook", json={})
            assert resp.status_code == 503
    finally:
        await application.close()


async def test_yookassa_reverify_failure_does_not_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Body claims succeeded; authoritative Payment.find_one disagrees.
    Spoofed-webhook defense: no credit, no idempotency row."""
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=False)
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_unreachable_merchant_api_asks_for_redelivery(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reverify that never finishes is not a verdict about the payment.

    The test above is the spoofed-webhook defense: the merchant API
    answered, and it said this payment did not succeed. This one is
    the opposite situation with the same old answer — the merchant API
    did not answer at all, because the socket died, the credentials
    were rejected, or (the shape prod is actually in) the optional
    ``yookassa`` package is not installed. The adapter spelled every
    one of those ``None``, and ``None`` on this route is acknowledged
    with 200.

    That ack is the bug. YooKassa's documented contract is that 200
    stops redelivery, so acking here discards a real payment on the
    strength of *our* network failing: the payer is charged, the coins
    never arrive, and the only trace is a WARNING. #1610 already made
    this call one branch over — the reverify *timeout* answers 503
    precisely because "we did not decide anything, nothing was
    written, so the honest answer is ask me again". A reverify that
    raises is the same outcome reached by a different route.
    """
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(
        monkeypatch, succeeded=True, raises=ConnectionError("merchant API unreachable")
    )
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 503, (
        "an unfinished reverify was acknowledged as if it were a verdict — "
        "YooKassa stops redelivering after 200, so this payment is now lost"
    )
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_without_the_optional_sdk_asks_for_redelivery(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same finding in the shape production would meet it in.

    ``yookassa`` is an optional dependency: it is absent from
    ``pyproject.toml``'s ``dependencies`` (only from mypy's
    ``ignore_missing_imports`` list) and absent from prod's
    site-packages. Whoever turns the provider on by filling in
    ``YOOKASSA_SHOP_ID`` / ``YOOKASSA_SECRET_KEY`` — the only thing
    the route checks before accepting callbacks — gets an
    ``ImportError`` on the first real notification. Answering 200 to
    that would burn every payment made before somebody read the logs.
    """
    await _seed_wallet(application, 42, balance=100)
    # No fake installed, and the real package is not importable.
    monkeypatch.delitem(sys.modules, "yookassa", raising=False)
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 503
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_non_succeeded_event_ignored(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    payload = _yookassa_payload()
    payload["event"] = "payment.canceled"
    payload["object"]["status"] = "canceled"
    resp = client.post("/yookassa-webhook", json=payload)
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100


# ---------------------------------------------------------------------------
# #300 — a body that disagrees with itself
#
# YooKassa does not sign its callbacks, so both the envelope event and
# the object status are attacker-supplied. The adapter accepts either
# one as evidence of a success, because the bare-object shape has no
# envelope to read — but a body that carries both and contradicts itself
# describes nothing real, and is refused before it can spend a
# merchant-API round-trip.
# ---------------------------------------------------------------------------


async def test_yookassa_body_contradicting_itself_is_refused_before_reverify(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``payment.succeeded`` wrapped around a cancelled object.

    The reverify would have vouched for this payment — the stub is
    installed with ``succeeded=True`` on purpose, so the only thing
    standing between this body and a credit is the gate itself. The
    ``calls`` list is the sharper half of the assertion: the point is
    not merely that no coins moved, it is that an unauthenticated POST
    never got to make this process issue a blocking outbound HTTPS
    request.
    """
    await _seed_wallet(application, 42, balance=100)
    calls: list[str] = []
    _install_fake_yookassa(monkeypatch, succeeded=True, calls=calls)
    payload = _yookassa_payload()
    payload["object"]["status"] = "canceled"

    resp = client.post("/yookassa-webhook", json=payload)

    assert resp.status_code == 200
    assert calls == [], "a self-contradicting body reached Payment.find_one"
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_canceled_envelope_over_succeeded_object_is_refused(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same contradiction the other way round.

    Worth pinning separately: the status arm alone would have accepted
    this body, so it is the direction where over-trusting the object
    costs something.
    """
    await _seed_wallet(application, 42, balance=100)
    calls: list[str] = []
    _install_fake_yookassa(monkeypatch, succeeded=True, calls=calls)
    payload = _yookassa_payload()
    payload["event"] = "payment.canceled"

    resp = client.post("/yookassa-webhook", json=payload)

    assert resp.status_code == 200
    assert calls == []
    assert await _wallet_balance(application, 42) == 100


async def test_yookassa_bare_payment_object_still_credits(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#300 must not tighten the gate into refusing the envelope-less shape.

    ``parse_event`` falls back to treating the whole body as the payment
    object when there is no ``object`` key. Such a body has a status and
    no event at all, so requiring both fields to agree would have to mean
    requiring both to be *present* — and that would silently drop this
    shape. It is the regression this test exists to catch.
    """
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    bare = _yookassa_payload()["object"]

    resp = client.post("/yookassa-webhook", json=bare)

    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100 + 4990


async def test_yookassa_inflated_metadata_coins_credits_server_derived_value(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-003: a payload claiming ``metadata.coins=999999999`` for
    a 499 RUB payment must NOT credit the inflated value. The
    server-derived amount (499 RUB * 10 coins/RUB == 4990) wins.

    Defense-in-depth: the YooKassa webhook itself is unauthenticated
    (we reverify via ``Payment.find_one``), but even after reverify
    the metadata is *not* re-checked by the provider — it was
    attached at session-creation time and travels through. Trusting
    it lets any code path that influences metadata (or any bug that
    sets it wrong) mint coins directly.
    """
    await _seed_wallet(application, 42, balance=0)
    # The inflated count is planted on the *reverified* record — since
    # R11-b the body's copy is ignored outright, so planting it there
    # would no longer exercise anything.
    _install_fake_yookassa(monkeypatch, succeeded=True, coins="999999999")
    resp = client.post(
        "/yookassa-webhook",
        json=_yookassa_payload(rub="499.00", coins="999999999"),
    )
    assert resp.status_code == 200
    # Wallet credited with the server-derived value, NOT the inflated
    # metadata one.
    assert await _wallet_balance(application, 42) == 4990


async def test_yookassa_missing_amount_value_does_not_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-003: without a parseable ``amount.value`` the adapter
    can't derive coins safely, so the event is rejected (no credit,
    no idempotency row). Previously the adapter would still credit
    using ``metadata.coins`` alone.
    """
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True, rub="")  # missing value
    payload = _yookassa_payload()
    payload["object"]["amount"] = {"currency": "RUB"}
    resp = client.post("/yookassa-webhook", json=payload)
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_replayed_body_cannot_inflate_the_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-020 R11-b, end to end: the balance follows the real payment.

    YooKassa notifications are unsigned, so the payer of a genuine
    100 RUB payment could POST the notification themselves with the
    amount and recipient rewritten. Reverify authenticates the *id*;
    it has never authenticated the body's numbers. Both rewrites are
    discarded here — the wallet moves by what was actually paid, and
    the attacker's wallet does not move at all.
    """
    await _seed_wallet(application, 42, balance=0)
    await _seed_wallet(application, 9999, balance=0)
    _install_fake_yookassa(monkeypatch, succeeded=True, rub="100.00", user_id="42")
    payload = _yookassa_payload()
    payload["object"]["amount"] = {"value": "1000000.00", "currency": "RUB"}
    payload["object"]["metadata"] = {"user_id": "9999", "coins": "10000000"}
    resp = client.post("/yookassa-webhook", json=payload)
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 1000  # 100 RUB at the anchor
    assert await _wallet_balance(application, 9999) == 0


async def test_yookassa_non_rouble_settlement_does_not_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """499 USD priced as 499 RUB would sell coins at a 90x discount."""
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True, currency="USD")
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity"])
async def test_yookassa_non_finite_amount_does_not_crash_the_webhook(
    application: Application,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    bad_amount: str,
) -> None:
    """``Decimal`` parses both without raising; one then raises on
    comparison and the other at ``int()``. Neither may reach the
    arithmetic, and neither may turn into a 500."""
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True, rub=bad_amount)
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_yookassa_unknown_user_acknowledged(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_yookassa(monkeypatch, succeeded=True)
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 200
    # #770: no wallet existed, so one was seeded and the payment landed.
    # The idempotency row is the proof the credit completed — before
    # #770 this asserted 0, i.e. a settled payment with nothing to show.
    assert await _processed_count(application, "yookassa") == 1


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


def _install_fake_stripe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid: bool,
    event_payload: dict[str, Any] | None = None,
) -> None:
    fake = types.ModuleType("stripe")

    class _Webhook:
        @staticmethod
        def construct_event(body: bytes, sig: str, secret: str) -> dict[str, Any]:
            if not valid:
                raise ValueError("invalid signature")
            assert event_payload is not None
            return event_payload

    fake.Webhook = _Webhook  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", fake)


def _stripe_event(
    session_id: str = "cs_test_123",
    *,
    amount_total: int = 1000,  # cents → $10 → 9000 coins at 900 coins/USD
    coins: str = "9000",
) -> dict[str, Any]:
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "amount_total": amount_total,
                "metadata": {"user_id": "55", "coins": coins},
            }
        },
    }


async def test_stripe_valid_credits_wallet(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_wallet(application, 55, balance=100)
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    resp = client.post(
        "/stripe-webhook",
        content=b"{}",
        headers={"content-type": "application/json", "Stripe-Signature": "t=1,v1=a"},
    )
    assert resp.status_code == 200
    assert resp.content == b""
    assert await _wallet_balance(application, 55) == 100 + 9000
    assert await _processed_count(application, "stripe") == 1


async def test_stripe_duplicate_delivery_does_not_double_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_wallet(application, 55, balance=100)
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    headers = {"content-type": "application/json", "Stripe-Signature": "t=1,v1=a"}
    r1 = client.post("/stripe-webhook", content=b"{}", headers=headers)
    r2 = client.post("/stripe-webhook", content=b"{}", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert await _wallet_balance(application, 55) == 100 + 9000
    assert await _processed_count(application, "stripe") == 1


async def test_stripe_inflated_metadata_coins_credits_server_derived_value(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-003: even with a valid HMAC signature, the metadata
    ``coins`` field is set at Checkout-Session-creation time and is
    not separately authenticated. An attacker who can influence the
    metadata (e.g. via a future redeem-code flow that pre-fills it,
    or a tampered checkout URL passing metadata through to the
    session) could mint coins. We derive coins from the signed
    ``amount_total`` instead.

    Repro: $10 charge (1000 cents) with metadata claiming 9_999_999
    coins. Wallet must be credited with the server-derived 9000.
    """
    await _seed_wallet(application, 55, balance=0)
    _install_fake_stripe(
        monkeypatch,
        valid=True,
        event_payload=_stripe_event(amount_total=1000, coins="9999999"),
    )
    resp = client.post(
        "/stripe-webhook",
        content=b"{}",
        headers={"content-type": "application/json", "Stripe-Signature": "t=1,v1=a"},
    )
    assert resp.status_code == 200
    assert await _wallet_balance(application, 55) == 9000


async def test_stripe_missing_amount_total_does_not_credit(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-003: without a parseable ``amount_total`` we can't
    derive coins, so the event is rejected (no credit, no
    idempotency row). Previously the adapter would still credit
    from ``metadata.coins`` alone.
    """
    await _seed_wallet(application, 55, balance=100)
    event = _stripe_event()
    event["data"]["object"].pop("amount_total")  # type: ignore[index]
    _install_fake_stripe(monkeypatch, valid=True, event_payload=event)
    resp = client.post(
        "/stripe-webhook",
        content=b"{}",
        headers={"content-type": "application/json", "Stripe-Signature": "t=1,v1=a"},
    )
    assert resp.status_code == 200
    assert await _wallet_balance(application, 55) == 100
    assert await _processed_count(application, "stripe") == 0


async def test_stripe_bad_signature_returns_400(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_stripe(monkeypatch, valid=False)
    resp = client.post(
        "/stripe-webhook",
        content=b"{}",
        headers={"content-type": "application/json", "Stripe-Signature": "bogus"},
    )
    assert resp.status_code == 400


async def test_stripe_missing_config_returns_503(tmp_path: Path) -> None:
    application = await _build_application(_settings(tmp_path, stripe=False))
    try:
        fastapi_app = create_app(application)
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/stripe-webhook",
                content=b"{}",
                headers={"content-type": "application/json", "Stripe-Signature": "x"},
            )
            assert resp.status_code == 503
    finally:
        await application.close()


async def test_stripe_non_checkout_event_acknowledged(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_wallet(application, 55, balance=100)
    event = _stripe_event()
    event["type"] = "payment_intent.created"
    _install_fake_stripe(monkeypatch, valid=True, event_payload=event)
    resp = client.post(
        "/stripe-webhook",
        content=b"{}",
        headers={"content-type": "application/json", "Stripe-Signature": "x"},
    )
    assert resp.status_code == 200
    assert await _wallet_balance(application, 55) == 100


# ---------------------------------------------------------------------------
# M-E-5: DM-send failure observability
# ---------------------------------------------------------------------------


async def test_dm_failure_after_commit_emits_counter_and_log(
    application: Application,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-E-5: when the post-commit DM raises, the credit still lands,
    the webhook still answers 200, and the failure shows up in both
    Prometheus and the structured log so an operator can tell "credit
    landed, user missed the receipt" apart from "credit silently
    never happened".
    """
    from aiogram.exceptions import TelegramForbiddenError

    await _seed_wallet(application, 777, balance=100)

    async def _raise_forbidden(*_args: Any, **_kwargs: Any) -> None:
        # Mirrors aiogram's real raise on a blocked user. The bot
        # method signature is async, so the exception must surface
        # from the awaitable, not the call site.
        raise TelegramForbiddenError(method=object(), message="user blocked")  # type: ignore[arg-type]

    fastapi_app = create_app(application)
    fastapi_app.state.application.bot.send_message = _raise_forbidden  # type: ignore[method-assign]

    before = PAYMENT_DM_FAILURES.labels(provider="crypto")._value.get()  # type: ignore[attr-defined]

    # Route loguru → standard logging so caplog captures the
    # structured record. Without this, loguru writes to its own sinks
    # and caplog stays empty.
    import logging

    from loguru import logger as _loguru

    handler_id = _loguru.add(
        lambda msg: logging.getLogger("loguru").info(msg.record["message"]),
        level="ERROR",
    )
    try:
        with caplog.at_level(logging.INFO, logger="loguru"), TestClient(fastapi_app) as c:
            body = json.dumps(_crypto_payload()).encode()
            resp = c.post(
                "/crypto-webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": _crypto_sign(body),
                },
            )
    finally:
        _loguru.remove(handler_id)

    # Webhook stayed 200 — DM failure must not cascade into a retry.
    assert resp.status_code == 200
    # Credit landed despite DM failure: balance moved, ledger row,
    # processed_webhooks row all present.
    assert await _wallet_balance(application, 777) == 100 + 900
    assert await _processed_count(application, "crypto") == 1
    # Counter bumped once for this provider.
    after = PAYMENT_DM_FAILURES.labels(provider="crypto")._value.get()  # type: ignore[attr-defined]
    assert after == before + 1
    # Structured log message present.
    assert any("payment_dm_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Credit-side failure observability (PAYMENT_CREDIT_FAILURES)
# ---------------------------------------------------------------------------


async def test_a_refused_credit_bumps_the_failure_counter(
    application: Application, client: TestClient
) -> None:
    """A verified event the economy layer refuses is acked 200 but the
    money never lands — that terminal non-credit outcome must bump
    ``tib_payment_credit_failures_total{provider=crypto,reason=credit_refused}``
    so it's not invisible outside the log.

    The trigger used to be "this user has no wallet". #770 made that
    case self-heal, so the refusal here is driven by the balance
    ceiling instead — the cause that actually survives.
    """
    from telegram_invite_bot.webhook.metrics import PAYMENT_CREDIT_FAILURES

    await _seed_wallet(application, 777, balance=_MAX_AMOUNT)
    before = PAYMENT_CREDIT_FAILURES.labels(  # type: ignore[attr-defined]
        provider="crypto", reason="credit_refused"
    )._value.get()

    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )
    assert resp.status_code == 200
    assert await _wallet_balance(application, 777) == _MAX_AMOUNT
    after = PAYMENT_CREDIT_FAILURES.labels(  # type: ignore[attr-defined]
        provider="crypto", reason="credit_refused"
    )._value.get()
    assert after == before + 1


async def test_pipeline_crash_bumps_credit_failure_counter(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the credit transaction raises (DB fault, bug), the route
    swallows it to 200 (so a poison event doesn't loop) but bumps
    ``tib_payment_credit_failures_total{reason=pipeline_crash}`` so the
    silent failure is observable.
    """
    from telegram_invite_bot.webhook import payments as payments_module
    from telegram_invite_bot.webhook.metrics import PAYMENT_CREDIT_FAILURES

    await _seed_wallet(application, 777, balance=100)

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated credit pipeline fault")

    monkeypatch.setattr(payments_module, "_credit_event", _boom)

    before = PAYMENT_CREDIT_FAILURES.labels(  # type: ignore[attr-defined]
        provider="crypto", reason="pipeline_crash"
    )._value.get()

    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )
    assert resp.status_code == 200
    after = PAYMENT_CREDIT_FAILURES.labels(  # type: ignore[attr-defined]
        provider="crypto", reason="pipeline_crash"
    )._value.get()
    assert after == before + 1


# ---------------------------------------------------------------------------
# A transient DB fault must NOT be acked — the provider has to retry
# ---------------------------------------------------------------------------
#
# The counterpart to the test above. Acking 200 on *any* exception was
# right for a deterministic fault (a poison event fails identically on
# every redelivery, so retries only burn the ladder) and wrong for a
# transient one: the transaction rolled back, nothing was credited, and
# 200 tells the provider to forget a payment the customer actually made.
# The user is out real money and the only trace is a log line.
#
# "database is locked" is not hypothetical here — the credit path takes
# the SQLite write lock while other handlers hold it, and SQLITE_BUSY
# can outlive ``busy_timeout`` under contention. SQLAlchemy surfaces it
# as ``OperationalError``.


def _rollypay_payload(payment_id: str = "PAY-1") -> dict[str, Any]:
    """A minimal ``payment.paid`` callback the adapter accepts.

    ``test`` is absent on purpose — a sandbox payment is signed with
    the same secret and the adapter refuses to credit it.
    """
    return {
        "event_type": "payment.paid",
        "payment_id": payment_id,
        "currency": "RUB",
        "amount": "1000.00",
        "metadata": {"user_id": 55},
    }


def _rollypay_headers(body: bytes) -> dict[str, str]:
    """Sign ``body`` the way RollyPay does: HMAC over ``ts + "." + body``."""
    ts = str(int(time.time()))
    signature = hmac.new(
        _ROLLYPAY_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "content-type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": signature,
    }


def _db_locked() -> OperationalError:
    """The exact exception SQLAlchemy wraps SQLITE_BUSY in."""
    return OperationalError("UPDATE wallets SET balance = ?", (), Exception("database is locked"))


def _raise_db_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the whole credit pipeline fail as if the DB were busy."""
    from telegram_invite_bot.webhook import payments as payments_module

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise _db_locked()

    monkeypatch.setattr(payments_module, "_credit_event", _boom)


async def test_crypto_transient_db_fault_asks_for_a_retry(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crypto Pay redelivers on non-2xx — so answer 503, not 200."""
    from telegram_invite_bot.webhook.metrics import PAYMENT_CREDIT_FAILURES

    await _seed_wallet(application, 777, balance=100)
    _raise_db_locked(monkeypatch)

    before = PAYMENT_CREDIT_FAILURES.labels(  # type: ignore[attr-defined]
        provider="crypto", reason="db_unavailable"
    )._value.get()

    body = json.dumps(_crypto_payload()).encode()
    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )

    assert resp.status_code == 503
    assert resp.json() == {"ok": False, "error": "temporary_failure"}
    after = PAYMENT_CREDIT_FAILURES.labels(  # type: ignore[attr-defined]
        provider="crypto", reason="db_unavailable"
    )._value.get()
    assert after == before + 1
    # Nothing landed, which is exactly why the retry is safe.
    assert await _wallet_balance(application, 777) == 100
    assert await _processed_count(application, "crypto") == 0


async def test_yookassa_transient_db_fault_asks_for_a_retry(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YooKassa retries for ~24h on any non-200. Let it."""
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    _raise_db_locked(monkeypatch)

    resp = client.post(
        "/yookassa-webhook",
        content=json.dumps(_yookassa_payload()).encode(),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 503
    assert await _wallet_balance(application, 42) == 100


async def test_stripe_transient_db_fault_asks_for_a_retry(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe retries on 5xx and NOT on 4xx — which is why this is 503.

    The signature branch stays 400 on purpose (a forged event must die
    on the first delivery); only a busy database earns a redelivery.
    """
    await _seed_wallet(application, 55, balance=100)
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    _raise_db_locked(monkeypatch)

    resp = client.post(
        "/stripe-webhook",
        content=b"{}",
        headers={"content-type": "application/json", "Stripe-Signature": "t=1,v1=a"},
    )

    assert resp.status_code == 503
    assert await _wallet_balance(application, 55) == 100


async def test_rollypay_transient_db_fault_asks_for_a_retry(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 8-attempt ladder exists for exactly this case."""
    await _seed_wallet(application, 55, balance=100)
    _raise_db_locked(monkeypatch)

    body = json.dumps(_rollypay_payload()).encode()
    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))

    assert resp.status_code == 503
    assert await _wallet_balance(application, 55) == 100


async def test_rollypay_valid_credits_wallet(application: Application, client: TestClient) -> None:
    """Baseline for the two tests above: the same request DOES credit.

    Without this the 503 tests would still pass if the request never
    reached the credit pipeline at all (unsigned, unconfigured, parsed
    to None) — they would be asserting nothing.
    """
    await _seed_wallet(application, 55, balance=100)

    body = json.dumps(_rollypay_payload()).encode()
    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))

    assert resp.status_code == 200
    balance = await _wallet_balance(application, 55)
    assert balance is not None
    assert balance > 100
    assert await _processed_count(application, "rollypay") == 1


async def test_rollypay_credit_records_the_rouble_charge(
    application: Application, client: TestClient
) -> None:
    """#239: end-to-end, the ₽ figure survives all the way to the row.

    The unit tests prove the adapter builds the fields and the service
    test proves the repo writes them; neither proves the router wires
    the two together. This is the assertion that would have caught the
    original bug — three real RollyPay credits landed in production
    with the coin amount recorded and the rouble amount lost.

    ``fx_rate`` is asserted only for presence: the live rate is
    resolved per request and pinning it here would test the FX service,
    not the plumbing. What matters is that *some* rate was recorded, so
    the coin figure can be re-derived from the rouble figure later.
    """
    await _seed_wallet(application, 55, balance=100)

    body = json.dumps(_rollypay_payload()).encode()
    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))
    assert resp.status_code == 200

    async with application.engines.session(DBName.ECONOMY)() as s:
        row = await s.execute(
            select(ProcessedWebhook).where(ProcessedWebhook.provider == "rollypay")
        )
        pw = row.scalars().one()
    assert pw.fiat_amount == "1000.00"
    assert pw.fiat_currency == "RUB"
    assert pw.fx_rate is not None


async def test_rollypay_duplicate_delivery_does_not_double_credit(
    application: Application, client: TestClient
) -> None:
    """A redelivery of a credited payment is a no-op, not a second payout."""
    await _seed_wallet(application, 55, balance=100)

    body = json.dumps(_rollypay_payload()).encode()
    headers = _rollypay_headers(body)
    first = client.post("/rollypay-webhook", content=body, headers=headers)
    after_first = await _wallet_balance(application, 55)
    second = client.post("/rollypay-webhook", content=body, headers=headers)

    assert first.status_code == second.status_code == 200
    assert await _wallet_balance(application, 55) == after_first
    assert await _processed_count(application, "rollypay") == 1


# ---------------------------------------------------------------------------
# T-020 R11 — the rouble leg, priced through the dollar anchor
# ---------------------------------------------------------------------------


async def test_yookassa_credit_follows_the_live_fx_fix(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weak rouble buys fewer coins, end to end through the webhook.

    The whole chain is exercised here rather than at the adapter: the
    route has to resolve the fix in async land and hand it to a sync
    adapter, and it is the *credited balance* — not a return value —
    that the owner's wallet actually feels.
    """

    async def _fx(_self: CurrencyService) -> dict[str, float]:
        # ``base[code]`` is COM-denominated; USD/RUB = 120 is the ratio.
        return {"USD": 1 / 900, "RUB": 120 / 900}

    monkeypatch.setattr(CurrencyService, "_base_rates", _fx)
    _install_fake_yookassa(monkeypatch, succeeded=True, rub="120.00", coins="1200")
    await _seed_wallet(application, 42, balance=0)

    fastapi_app = create_app(application)
    fastapi_app.state.application.bot.send_message = _async_noop  # type: ignore[method-assign]
    with TestClient(fastapi_app) as c:
        # 120 RUB is exactly one dollar at this fix.
        resp = c.post("/yookassa-webhook", json=_yookassa_payload(rub="120.00"))

    assert resp.status_code == 200
    # One dollar in, one dollar's worth of coins out. Pre-R11 this
    # credited 1 200 — a third more than the /withdraw desk sells a
    # dollar for, redeemable immediately.
    assert await _wallet_balance(application, 42) == 900


async def test_yookassa_credit_survives_a_dead_fx_upstream(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An FX outage must not refuse a payment — it prices at the anchor.

    A top-up is real money already taken from the payer; failing the
    credit because a free FX endpoint had a bad minute would be the
    worst possible trade. The offline anchor is the price the bot used
    for its entire life before R11, so the degraded path is simply the
    old behaviour.
    """

    async def _boom(_self: CurrencyService) -> dict[str, float]:
        raise RuntimeError("exchangerate-api is down")

    monkeypatch.setattr(CurrencyService, "_base_rates", _boom)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    await _seed_wallet(application, 42, balance=0)

    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 4990


async def test_yookassa_credit_ignores_an_implausible_fx_quote(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken upstream that returns USD/RUB = 1 would credit 900 coins
    per rouble. The sanity band refuses it before it can mint."""

    async def _garbage(_self: CurrencyService) -> dict[str, float]:
        return {"USD": 1 / 900, "RUB": 1 / 900}  # USD/RUB == 1.0

    monkeypatch.setattr(CurrencyService, "_base_rates", _garbage)
    _install_fake_yookassa(monkeypatch, succeeded=True)
    await _seed_wallet(application, 42, balance=0)

    fastapi_app = create_app(application)
    fastapi_app.state.application.bot.send_message = _async_noop  # type: ignore[method-assign]
    with TestClient(fastapi_app) as c:
        resp = c.post("/yookassa-webhook", json=_yookassa_payload())

    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 4990  # anchor, not 449_100


# ---------------------------------------------------------------------------
# Work order on a public endpoint: verify first, then spend
# ---------------------------------------------------------------------------
#
# ``/rollypay-webhook`` is reachable by anyone on the internet. The FX
# lookup behind it is hour-cached and warmed at startup, so the cost of
# resolving it early is usually nil — but "usually" is doing real work
# in that sentence. There is no stampede guard around the cache fill, so
# a burst of forged callbacks timed at a TTL boundary would each miss and
# fan out their own request against a metered key, and each would sit on
# the FX timeout before finally answering 403. None of that should be
# purchasable by a caller who cannot produce a valid HMAC.


def _count_fx_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every FX resolution the router performs, without the wire.

    Patched in the *router's* namespace, because the ordering under test
    is the router's — not the currency service's caching behaviour.
    """
    from telegram_invite_bot.services.payments.rates import FALLBACK_USD_TO_RUB
    from telegram_invite_bot.webhook import payments as payments_module

    calls: list[object] = []

    async def _counting(service: Any) -> float:
        calls.append(service)
        return FALLBACK_USD_TO_RUB

    monkeypatch.setattr(payments_module, "resolve_usd_to_rub", _counting)
    return calls


async def test_rollypay_forged_signature_never_reaches_the_fx_lookup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that fails the HMAC must cost nothing but the digest."""
    calls = _count_fx_calls(monkeypatch)

    body = json.dumps(_rollypay_payload()).encode()
    headers = _rollypay_headers(body)
    headers["X-Signature"] = "0" * 64  # right shape, wrong digest

    resp = client.post("/rollypay-webhook", content=body, headers=headers)

    assert resp.status_code == 403
    assert calls == [], "FX was resolved for a caller that failed verification"


async def test_rollypay_absurd_timestamp_is_refused_not_crashed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1698 — the freshness check used to be reachable as a 500.

    ``X-Timestamp`` is read before the HMAC, because the raw header is
    part of what gets signed. It was parsed with ``int()`` guarded by
    ``ValueError``, and a long run of ASCII digits satisfies both:
    CPython only refuses the conversion at 4300 digits. The skew
    subtraction that followed then raised ``OverflowError: int too large
    to convert to float`` — not a ``ValueError``, not caught anywhere,
    and reachable by anyone on the internet with no knowledge of the
    signing secret, one header long. It answers 403 now, like every
    other unusable timestamp.
    """
    calls = _count_fx_calls(monkeypatch)

    body = json.dumps(_rollypay_payload()).encode()
    headers = _rollypay_headers(body)
    headers["X-Timestamp"] = "1" + "0" * 400

    resp = client.post("/rollypay-webhook", content=body, headers=headers)

    assert resp.status_code == 403
    assert calls == [], "FX was resolved for a caller that failed verification"


async def test_rollypay_valid_signature_still_prices_off_the_live_fix(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: deleting the lookup would satisfy the test above.

    A genuine callback must still resolve the fix — exactly once — or
    the rouble leg silently reverts to the pre-R11 anchor price.
    """
    calls = _count_fx_calls(monkeypatch)
    await _seed_wallet(application, 55, balance=100)

    body = json.dumps(_rollypay_payload()).encode()
    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))

    assert resp.status_code == 200
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# A reversal is not a payment, however its body spells the status
# ---------------------------------------------------------------------------
#
# ``parse_event`` accepts a credit when *either* ``event_type`` is
# ``payment.paid`` OR ``status`` is ``"paid"``. The second arm is the
# fallback for a callback that omits the event type — but a refund or
# chargeback describes a payment that *was* paid, so its body can carry
# ``status: "paid"`` quite honestly. Read as a credit, that costs twice:
# the router raises the owner's reversal alert only when parse_event
# returns None, so the alert goes silent; and the credit is a no-op only
# for as long as the reversal reuses the original payment id. Keyed by a
# refund id instead, money leaving the merchant account mints coins.


@pytest.mark.parametrize("event_type", sorted(REVERSAL_EVENTS))
async def test_rollypay_reversal_claiming_paid_status_does_not_credit(
    application: Application,
    client: TestClient,
    event_type: str,
) -> None:
    await _seed_wallet(application, 55, balance=100)

    payload = _rollypay_payload(payment_id="REFUND-1")
    payload["event_type"] = event_type
    payload["status"] = "paid"  # truthful about the payment, not the event
    body = json.dumps(payload).encode()

    before = PAYMENT_REVERSALS.labels(  # type: ignore[attr-defined]
        provider="rollypay", event=event_type
    )._value.get()

    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))

    assert resp.status_code == 200
    # Not a single coin — and since #226 the refund IS written down, as
    # a tombstone: a row that credited nothing, already stamped as
    # reversed. Its whole job is to be in the way of the ``paid`` retry
    # that this refund overtook.
    assert await _wallet_balance(application, 55) == 100
    assert await _processed_count(application, "rollypay") == 1
    tombstone = await _credit_record(application, "rollypay", "REFUND-1")
    assert tombstone is not None
    assert tombstone.credited_amount == 0
    assert tombstone.user_id == 0
    assert tombstone.reversed_at is not None
    assert tombstone.reversed_event == event_type
    # And the owner actually hears about it — the half that used to be
    # swallowed silently.
    after = PAYMENT_REVERSALS.labels(  # type: ignore[attr-defined]
        provider="rollypay", event=event_type
    )._value.get()
    assert after == before + 1, "the reversal alert never fired"


async def test_rollypay_status_fallback_still_credits_a_paid_callback(
    application: Application, client: TestClient
) -> None:
    """The narrow fix must not take the fallback down with it.

    A callback with no ``event_type`` at all still credits off
    ``status``; that arm is why the reversal check had to be a
    targeted exclusion rather than "trust event_type only".
    """
    await _seed_wallet(application, 55, balance=100)

    payload = _rollypay_payload()
    del payload["event_type"]
    payload["status"] = "paid"
    body = json.dumps(payload).encode()

    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))

    assert resp.status_code == 200
    balance = await _wallet_balance(application, 55)
    assert balance is not None
    assert balance > 100


async def test_an_unknown_rollypay_event_claiming_paid_credits_nothing(
    application: Application, client: TestClient
) -> None:
    """#227: the credit gate is an allowlist, not a list of exceptions.

    Naming the three reversal events and letting every other name
    through left the ``status`` fallback reachable for anything RollyPay
    had not invented yet. ``payment.partially_refunded`` is the shape
    that costs the most: it is a reversal in everything but its name,
    it carries ``status: "paid"`` because it describes a payment that
    was paid, and under the old gate it minted coins for money on its
    way back to the buyer.
    """
    await _seed_wallet(application, 55, balance=100)

    payload = _rollypay_payload(payment_id="PAY-227")
    payload["event_type"] = "payment.partially_refunded"
    payload["status"] = "paid"
    body = json.dumps(payload).encode()

    resp = client.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))

    assert resp.status_code == 200
    assert await _wallet_balance(application, 55) == 100
    # And nothing was written down either — the gate refused before the
    # idempotency row, so a name RollyPay later teaches us to credit is
    # not permanently barred by this delivery.
    assert await _processed_count(application, "rollypay") == 0


# ---------------------------------------------------------------------------
# Body-size ceiling (#138)
# ---------------------------------------------------------------------------


def _oversized_json() -> bytes:
    """A well-formed JSON document past ``PAYMENT_WEBHOOK_MAX_BYTES``."""
    filler = "x" * (PAYMENT_WEBHOOK_MAX_BYTES + 1024)
    return json.dumps({"filler": filler}).encode()


@pytest.mark.parametrize(
    "route",
    ["/crypto-webhook", "/yookassa-webhook", "/stripe-webhook", "/rollypay-webhook"],
)
async def test_oversized_callback_is_refused_by_every_route(
    application: Application, client: TestClient, route: str
) -> None:
    """413 on every provider, and nothing behind the gate ran."""
    await _seed_wallet(application, 777, balance=100)

    resp = client.post(
        route,
        content=_oversized_json(),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413
    assert await _wallet_balance(application, 777) == 100
    for provider in ("crypto", "yookassa", "stripe", "rollypay"):
        assert await _processed_count(application, provider) == 0


@pytest.mark.parametrize(
    "route",
    ["/crypto-webhook", "/yookassa-webhook", "/stripe-webhook", "/rollypay-webhook"],
)
async def test_oversized_chunked_callback_is_refused_by_every_route(
    application: Application, client: TestClient, route: str
) -> None:
    """#229: the same body, sent without declaring its length.

    A chunked request carries no ``Content-Length``, so the header gate
    above cannot see it and ``await request.body()`` used to buffer the
    whole thing before the signature was ever computed — an anonymous
    caller picking how much memory the single-process bot spends on
    rejecting them. The stream cap is what closes it, and it only means
    anything if the request is genuinely chunked, hence the assert on
    the outgoing headers.
    """
    await _seed_wallet(application, 778, balance=100)

    def stream() -> Iterator[bytes]:
        yield _oversized_json()

    resp = client.post(
        route,
        content=stream(),
        headers={"content-type": "application/json"},
    )

    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 413
    assert await _wallet_balance(application, 778) == 100
    for provider in ("crypto", "yookassa", "stripe", "rollypay"):
        assert await _processed_count(application, provider) == 0


@pytest.mark.parametrize("route", ["/crypto-webhook", "/rollypay-webhook"])
async def test_a_high_byte_in_the_signature_is_a_403_not_a_500(
    application: Application, client: TestClient, route: str
) -> None:
    """#916: one 0xFF in the signature header used to hand out a traceback.

    Starlette decodes header values with latin-1, so a byte in
    0x80-0xFF arrives at the adapter as a non-ASCII ``str`` — and
    ``hmac.compare_digest`` raises ``TypeError`` on those rather than
    returning False. The only exception handler this app registers is
    for ``StarletteHTTPException`` (cms/notfound.py:226), so the
    TypeError escaped as a 500 with a full uvicorn traceback to an
    anonymous caller.

    Both signed routes are covered because both were affected, and the
    consequence is worst on the RollyPay one: it is live with real
    money, RollyPay retries a non-2xx eight times, and every one of
    those retries would have hit the same 500 — a genuine payment
    banked and never credited, with no ``_alert_uncredited`` to show
    for it, because that helper sits downstream of the signature gate.

    The timestamp is valid on purpose. A stale or missing one would
    make the RollyPay adapter refuse before it ever reached the digest
    comparison, and the test would pass against the unfixed code.
    """
    await _seed_wallet(application, 779, balance=100)

    resp = client.post(
        route,
        content=json.dumps(_rollypay_payload()).encode(),
        headers=[
            (b"content-type", b"application/json"),
            (b"x-timestamp", str(int(time.time())).encode()),
            (b"x-signature", b"\xff" * 8),
            (b"crypto-pay-api-signature", b"\xff" * 8),
        ],
    )

    assert resp.status_code == 403
    assert await _wallet_balance(application, 779) == 100
    for provider in ("crypto", "rollypay"):
        assert await _processed_count(application, provider) == 0


async def test_the_size_gate_runs_before_the_configuration_gate(
    tmp_path: Path,
) -> None:
    """A megabyte must not buy a database round trip first.

    The Crypto Pay route resolves its token from ``runtime_secrets``
    before it can answer 503, so a size check placed after that gate
    would let an anonymous caller drive a query per oversized request.
    Answering 413 here — not 503 — is the evidence it comes first.
    """
    application = await _build_application(_settings(tmp_path, crypto=False))
    try:
        with TestClient(create_app(application)) as c:
            resp = c.post(
                "/crypto-webhook",
                content=_oversized_json(),
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": "x",
                },
            )
            assert resp.status_code == 413
    finally:
        await application.close()


async def test_a_callback_just_under_the_ceiling_still_credits(
    application: Application, client: TestClient
) -> None:
    """The ceiling must not clip a real payload.

    Crypto Pay's own callback is under 2 KB; this one is padded to just
    inside the limit to prove the gate is a ceiling rather than a
    tightening of the format.
    """
    await _seed_wallet(application, 777, balance=100)

    payload = _crypto_payload()
    # Fill to a little under the cap: the padding is an unknown key the
    # adapter ignores, so what is tested is the size, not the parse.
    padding = PAYMENT_WEBHOOK_MAX_BYTES - len(json.dumps(payload).encode()) - 512
    payload["_padding"] = "x" * padding
    body = json.dumps(payload).encode()
    assert len(body) < PAYMENT_WEBHOOK_MAX_BYTES

    resp = client.post(
        "/crypto-webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "crypto-pay-api-signature": _crypto_sign(body),
        },
    )

    assert resp.status_code == 200
    balance = await _wallet_balance(application, 777)
    assert balance is not None
    assert balance > 100


# ---------------------------------------------------------------------------
# #140: a YooKassa refund is money leaving, and the owner must hear it
# ---------------------------------------------------------------------------
#
# The rouble provider with the *higher* chargeback exposure was the
# silent one: RollyPay counted and announced reversals, YooKassa dropped
# them into the same 200-and-forget branch as a benign lifecycle event.
# Worse, a refund object is itself ``status: "succeeded"``, so the
# fallback status arm inside ``parse_event`` would read it as a payment —
# harmless today only because the reverify is asked for a payment under
# a refund id, which is a coincidence of YooKassa's id namespaces rather
# than anything this code guarantees.


def _yookassa_refund_payload(
    *, refund_id: str = "REF-1", payment_id: str = "PAY-1", rub: str = "499.00"
) -> dict[str, Any]:
    """A completed refund exactly as YooKassa notifies it."""
    return {
        "type": "notification",
        "event": "refund.succeeded",
        "object": {
            "id": refund_id,
            "payment_id": payment_id,
            "status": "succeeded",
            "amount": {"value": rub, "currency": "RUB"},
        },
    }


def _reversal_count(provider: str, event: str) -> float:
    return PAYMENT_REVERSALS.labels(  # type: ignore[attr-defined]
        provider=provider, event=event
    )._value.get()


async def test_yookassa_refund_alerts_the_owner_and_credits_nothing(
    application: Application,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake reverify says "succeeded" for *any* id it is handed —
    which is the point. If the refund reached the reverify at all, this
    body would mint 4990 coins. Nothing may move, and the counter must
    tick.
    """
    await _seed_wallet(application, 42, balance=100)
    _install_fake_yookassa(monkeypatch, succeeded=True)

    before = _reversal_count("yookassa", "refund.succeeded")
    resp = client.post("/yookassa-webhook", json=_yookassa_refund_payload())

    assert resp.status_code == 200
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0
    assert _reversal_count("yookassa", "refund.succeeded") == before + 1


async def test_yookassa_canceled_payment_is_not_reported_as_a_reversal(
    application: Application,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled payment was never captured, so no coins were ever
    minted against it. Alerting on it would train the owner to ignore
    the alert that matters.
    """
    _install_fake_yookassa(monkeypatch, succeeded=False)
    payload = _yookassa_payload()
    payload["event"] = "payment.canceled"
    payload["object"]["status"] = "canceled"  # type: ignore[index]

    before = _reversal_count("yookassa", "payment.canceled")
    resp = client.post("/yookassa-webhook", json=payload)

    assert resp.status_code == 200
    assert _reversal_count("yookassa", "payment.canceled") == before


async def test_yookassa_refund_alert_names_the_payment_not_the_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DM must carry the id the owner can look a user up by.

    ``object.id`` is the refund's own id and appears nowhere in the
    payment history; ``object.payment_id`` is the top-up that minted the
    coins. Naming them the other way round sends the owner hunting in
    the wrong column.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    try:
        _install_fake_yookassa(monkeypatch, succeeded=True)
        sent: list[tuple[int, str]] = []

        async def _capture(chat_id: int, text: str, **_: object) -> None:
            sent.append((chat_id, text))

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/yookassa-webhook",
                json=_yookassa_refund_payload(refund_id="REF-9", payment_id="PAY-9"),
            )
    finally:
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 123456789
    assert "ЮKassa" in text
    assert "Платёж: <code>PAY-9</code>" in text
    assert "Возврат: <code>REF-9</code>" in text
    assert "499.00 RUB" in text


async def test_yookassa_reversal_alert_survives_a_body_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema surprise must not swallow the alert.

    The event is readable, the rest is not — the owner still gets a
    message, with placeholders where the ids would have been.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    try:
        _install_fake_yookassa(monkeypatch, succeeded=True)
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
            sent.append(text)

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/yookassa-webhook",
                json={"event": "refund.succeeded", "object": "not-an-object"},
            )
    finally:
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    assert "Платёж: <code>?</code>" in sent[0]
    assert "Сумма: <b>?</b>" in sent[0]


async def test_a_failed_reversal_dm_still_answers_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DM is the convenient half of the signal, the counter is the
    durable one. A blocked owner must not turn the webhook into a retry
    loop — YooKassa would redeliver the same refund for hours.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    try:
        _install_fake_yookassa(monkeypatch, succeeded=True)

        from aiogram.exceptions import TelegramForbiddenError

        async def _boom(*_: object, **__: object) -> None:
            raise TelegramForbiddenError(method=object(), message="blocked")  # type: ignore[arg-type]

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _boom  # type: ignore[method-assign]
        before = _reversal_count("yookassa", "refund.succeeded")
        with TestClient(fastapi_app) as c:
            resp = c.post("/yookassa-webhook", json=_yookassa_refund_payload())
    finally:
        await application.close()

    assert resp.status_code == 200
    assert _reversal_count("yookassa", "refund.succeeded") == before + 1


# ---------------------------------------------------------------------------
# #141: Stripe reversals (refund / dispute)
# ---------------------------------------------------------------------------

_STRIPE_SIG_HEADERS: Final[dict[str, str]] = {
    "content-type": "application/json",
    "Stripe-Signature": "t=1,v1=a",
}


def _stripe_refund_payload(
    *,
    event: str = "charge.refunded",
    object_id: str = "ch_1",
    payment_intent: str | None = "pi_1",
    minor: int | None = 49900,
    amount_field: str = "amount_refunded",
) -> dict[str, Any]:
    """A charge-refunded / dispute event as Stripe sends it.

    ``amount_field`` differs by shape: a refunded Charge reports
    ``amount_refunded`` (the part actually returned, which may be less
    than the charge), a Dispute reports ``amount``.
    """
    obj: dict[str, Any] = {"id": object_id, "currency": "usd"}
    if payment_intent is not None:
        obj["payment_intent"] = payment_intent
    if minor is not None:
        obj[amount_field] = minor
    return {"type": event, "data": {"object": obj}}


async def test_stripe_refund_alerts_the_owner_and_credits_nothing(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#141: the last silent provider.

    A refund is not a checkout completion, so it fell into the "200 and
    forget" branch. Nothing about the wallet changes — the coins are
    already spent or not, and only the owner can decide — but the event
    is counted and reported instead of vanishing.
    """
    await _seed_wallet(application, 55, balance=100)
    payload = _stripe_refund_payload()
    _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
    before = _reversal_count("stripe", "charge.refunded")

    resp = client.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)

    assert resp.status_code == 200
    assert await _wallet_balance(application, 55) == 100
    assert await _processed_count(application, "stripe") == 0
    assert _reversal_count("stripe", "charge.refunded") == before + 1


async def test_stripe_dispute_closed_is_not_reported_as_a_reversal(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispute can close *won* — money coming back, not leaving.

    Alerting on it would teach the owner to skim past the alert that
    matters, so ``charge.dispute.closed`` is deliberately outside
    ``REVERSAL_EVENTS``.
    """
    payload = _stripe_refund_payload(event="charge.dispute.closed")
    _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
    before = _reversal_count("stripe", "charge.dispute.closed")

    resp = client.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)

    assert resp.status_code == 200
    assert _reversal_count("stripe", "charge.dispute.closed") == before


async def test_stripe_dispute_alert_names_the_charge_and_the_sum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Dispute has no ``payment_intent`` — it points at ``charge``.

    And its sum is in minor units, so 49900 must reach the owner as
    499.00 USD rather than as a five-digit number that reads like the
    end of the business.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    try:
        payload = _stripe_refund_payload(
            event="charge.dispute.created",
            object_id="dp_9",
            payment_intent=None,
            amount_field="amount",
        )
        payload["data"]["object"]["charge"] = "ch_9"  # type: ignore[index]
        _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
        sent: list[tuple[int, str]] = []

        async def _capture(chat_id: int, text: str, **_: object) -> None:
            sent.append((chat_id, text))

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)
    finally:
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 123456789
    assert "Stripe" in text
    assert "charge.dispute.created" in text
    assert "Платёж: <code>ch_9</code>" in text
    assert "Событие: <code>dp_9</code>" in text
    assert "499.00 USD" in text


async def test_stripe_reversal_alert_survives_a_body_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The type is readable, the payload is not — still alert.

    Also pins the amount rule: a non-integer where minor units belong is
    a shape we do not recognise, and a wrong number in a money alert is
    worse than no number.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    try:
        payload: dict[str, Any] = {"type": "charge.refunded", "data": "not-an-object"}
        _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
            sent.append(text)

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)
    finally:
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    assert "Платёж: <code>?</code>" in sent[0]
    assert "Сумма: <b>?</b>" in sent[0]


async def test_stripe_reversal_is_not_reported_when_the_signature_fails(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert reads the body — so it must sit behind verification.

    Anyone can POST a refund-shaped body; only Stripe can sign one. An
    alert that fired before the HMAC check would be a free way to make
    the owner's phone ring, and worse, to make a real reversal
    indistinguishable from noise.
    """
    _install_fake_stripe(monkeypatch, valid=False)
    before = _reversal_count("stripe", "charge.refunded")

    resp = client.post(
        "/stripe-webhook",
        json=_stripe_refund_payload(),
        headers={"content-type": "application/json", "Stripe-Signature": "bogus"},
    )

    assert resp.status_code == 400
    assert _reversal_count("stripe", "charge.refunded") == before


async def test_a_giant_identifier_does_not_swallow_the_whole_alert(
    tmp_path: Path,
) -> None:
    """#142: a long field must cost the id, not the message.

    The body is signed, so this is not an attacker — it is a provider
    (or a merchant order id built by some upstream system) that does not
    share our idea of how long an identifier is. Interpolated whole, it
    pushes the DM past Telegram's 4096 characters, Telegram answers 400,
    the courtesy ``except`` logs it, and the owner hears nothing at all
    about money leaving the account. Truncated, they hear everything
    that matters.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    try:
        payload = _rollypay_payload(payment_id="P" * 5000)
        payload["event_type"] = "payment.refunded"
        payload["order_id"] = "O" * 5000
        body = json.dumps(payload).encode()
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
            if len(text) > 4096:
                raise AssertionError("Telegram would refuse this message")
            sent.append(text)

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))
    finally:
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    assert "PPPPP" in sent[0]
    assert "…" in sent[0]


# ---------------------------------------------------------------------------
# Paid, verified, and refused a credit (#144)
# ---------------------------------------------------------------------------


def _uncredited_count(provider: str) -> float:
    return PAYMENT_UNCREDITED.labels(provider=provider)._value.get()  # type: ignore[attr-defined]


def _credit_failure_count(provider: str, reason: str) -> float:
    """The ``(provider, reason)`` slot of the credit-failure counter.

    ``float(...)`` rather than a bare return: ``_value.get()`` is
    untyped, and the sibling readers in this file each pay for that with
    a pair of mypy errors. One more is one too many.
    """
    return float(PAYMENT_CREDIT_FAILURES.labels(provider=provider, reason=reason)._value.get())


async def _post_rollypay_capturing_dms(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    seed_wallet_for: int | None = None,
    seed_balance: int = 0,
) -> tuple[int, list[str]]:
    """POST one signed callback and collect whatever the owner was sent.

    The DM is the whole point of #144, and the shared ``application``
    fixture has no admin chat, so these cases build their own the way
    the #142 test does.

    ``seed_wallet_for`` gives the payer a wallet holding
    ``seed_balance`` before the POST. Left at ``None`` — which is what
    every #144 case wants, since those bodies never reach the credit
    pipeline — the payer has no wallet at all.

    Since #770 a missing wallet no longer makes the pipeline refuse: it
    seeds one. The #296 case therefore seeds a wallet already at the
    ceiling, which is the refusal that survived.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    try:
        if seed_wallet_for is not None:
            await _seed_wallet(application, seed_wallet_for, balance=seed_balance)
        body = json.dumps(payload).encode()
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))
    finally:
        await application.close()
    return resp.status_code, sent


async def test_a_crypto_leg_settled_outside_rub_reaches_the_owner(
    tmp_path: Path,
) -> None:
    """#144: money in, no coins out, and — until now — total silence.

    RollyPay's page takes crypto as well as card and SBP, so a callback
    reporting the crypto leg's own currency is the reachable version of
    this: the RUB gate refuses it (correctly — pricing USDT as roubles
    would under-credit ninefold), the route answers 200, and the payer
    is left paid and empty-handed with nothing but a WARNING to say so.
    """
    payload = _rollypay_payload(payment_id="PAY-CRYPTO")
    payload["currency"] = "USDT"
    payload["order_id"] = "ORD-77"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("rollypay") == before + 1, "nothing counted the loss"
    assert len(sent) == 1
    # The payment id is the one field that matches a dashboard row
    # against the user who is about to ask where their coins went.
    assert "PAY-CRYPTO" in sent[0]
    assert "ORD-77" in sent[0]


async def test_a_paid_callback_without_a_user_id_reaches_the_owner(
    tmp_path: Path,
) -> None:
    """The same hole through a different field.

    Nothing about the alert is currency-specific: any refusal that
    happens after the money cleared leaves the same debt.
    """
    payload = _rollypay_payload(payment_id="PAY-NOUSER")
    payload["metadata"] = {}

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("rollypay") == before + 1
    assert len(sent) == 1
    assert "PAY-NOUSER" in sent[0]


async def test_a_lifecycle_event_does_not_alarm_the_owner(tmp_path: Path) -> None:
    """Negative control, and the one that decides whether this is usable.

    ``payment.created`` and its siblings arrive for every single
    checkout. An alert that fires on them is an alert the owner mutes
    inside a day, which costs the reversal alerts their audience too.
    """
    payload = _rollypay_payload(payment_id="PAY-NEW")
    payload["event_type"] = "payment.created"
    payload["status"] = "pending"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("rollypay") == before
    assert sent == []


async def test_a_sandbox_payment_does_not_alarm_the_owner(tmp_path: Path) -> None:
    """A test payment is signed like a real one and owes nobody anything."""
    payload = _rollypay_payload(payment_id="PAY-SANDBOX")
    payload["currency"] = "USDT"
    payload["test"] = True

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("rollypay") == before
    assert sent == []


async def test_a_reversal_is_reported_once_and_as_a_reversal(tmp_path: Path) -> None:
    """The two alerts must not both fire on one body.

    A reversal callback legitimately carries ``status: "paid"`` — it
    describes a payment that was paid — so the naive "paid and not
    credited" reading would add a second, wrong alert saying coins are
    owed on money that just went back out.
    """
    payload = _rollypay_payload(payment_id="PAY-BACK")
    payload["event_type"] = "payment.refunded"
    payload["status"] = "paid"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("rollypay") == before
    assert len(sent) == 1
    assert "возврат средств" in sent[0]


async def test_an_unknown_paid_event_reaches_the_owner_instead_of_crediting(
    tmp_path: Path,
) -> None:
    """#227: refusing the credit is only half of it — it must be loud.

    An unlisted event type is refused by :meth:`parse_event` and is not
    in ``REVERSAL_EVENTS``, so neither of the two alerts above claims
    it. What catches it is ``describes_paid_money``, which stays
    permissive on the event type on purpose. Without that asymmetry the
    fix would trade minted coins for a payment that vanishes in
    silence, which is the harder failure to notice.
    """
    payload = _rollypay_payload(payment_id="PAY-UNKNOWN")
    payload["event_type"] = "payment.settled_v2"
    payload["status"] = "paid"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("rollypay") == before + 1
    assert len(sent) == 1
    assert "PAY-UNKNOWN" in sent[0]
    # The paid-but-uncredited card, not the reversal card: we do not
    # know that this event moved money back, only that we would not
    # credit it.
    assert "монеты НЕ зачислены" in sent[0]
    assert "возврат средств" not in sent[0]


async def test_an_unsigned_body_cannot_summon_the_uncredited_alert(
    tmp_path: Path,
) -> None:
    """The alert stays behind the HMAC, like every other read of the body.

    Otherwise the endpoint is a "make the owner's phone buzz" primitive
    for anyone who can reach a public URL.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    before = _uncredited_count("rollypay")
    try:
        payload = _rollypay_payload(payment_id="PAY-FORGED")
        payload["currency"] = "USDT"
        body = json.dumps(payload).encode()
        headers = _rollypay_headers(body)
        headers["X-Signature"] = "0" * 64
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post("/rollypay-webhook", content=body, headers=headers)
    finally:
        await application.close()

    assert resp.status_code == 403
    assert _uncredited_count("rollypay") == before
    assert sent == []


# ---------------------------------------------------------------------------
# #296 — the credit pipeline refused it, and that was silent too
#
# The three cases above all die in the *parser*. The one below gets
# further: signature good, event listed, currency RUB, user id present,
# amount priceable — and then ``PaymentsService`` answers
# ``CREDIT_REFUSED`` because the economy layer would not take the write.
# Until #296 that bumped ``PAYMENT_CREDIT_FAILURES`` and stopped, so a
# real 1000 ₽ went into the merchant account against a Prometheus series
# and nothing else.
# ---------------------------------------------------------------------------


async def test_a_credit_refused_after_verification_reaches_the_owner(
    tmp_path: Path,
) -> None:
    """#296: the money cleared, the pipeline said no, and nobody was told."""
    payload = _rollypay_payload(payment_id="PAY-CEILING")

    before_failures = _credit_failure_count("rollypay", "credit_refused")
    before_uncredited = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(
        tmp_path, payload, seed_wallet_for=55, seed_balance=_MAX_AMOUNT
    )

    assert code == 200
    assert _credit_failure_count("rollypay", "credit_refused") == before_failures + 1
    # The two series stay distinct, exactly as ``webhook/metrics.py``
    # documents: this event reached the pipeline, so it is a credit
    # failure and NOT a parser refusal.
    assert _uncredited_count("rollypay") == before_uncredited
    assert len(sent) == 1
    assert "монеты НЕ зачислены" in sent[0]
    # The payment id matches a dashboard row; the user id says who to
    # credit by hand. An alert missing either one is unactionable.
    assert "PAY-CEILING" in sent[0]
    assert "55" in sent[0]
    assert "credit_refused" in sent[0]


async def test_a_credit_that_lands_does_not_alarm_the_owner(tmp_path: Path) -> None:
    """Negative control, and the one that decides whether #296 is usable.

    The healthy path is every successful top-up. An alert that fires on
    those is an alert the owner mutes inside a day, which costs the
    reversal and parser-refusal alerts their audience too.
    """
    payload = _rollypay_payload(payment_id="PAY-OK296")

    before_failures = _credit_failure_count("rollypay", "credit_refused")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload, seed_wallet_for=55)

    assert code == 200
    assert _credit_failure_count("rollypay", "credit_refused") == before_failures
    assert all("монеты НЕ зачислены" not in text for text in sent)


# ---------------------------------------------------------------------------
# #174 — the reversal alert names the user, and the credit row remembers
#
# The bot has always known who a reversed payment belonged to:
# ``processed_webhooks`` stores (provider, external_id) → user_id +
# credited coins, and for RollyPay and YooKassa the reversal event
# carries that very same external_id. The alert still said only "some
# payment id came back", leaving the owner to grep a dashboard for the
# one fact the debit decision hinges on. Two halves are tested here —
# the sentence the owner reads, and the mark that outlives the process.
# ---------------------------------------------------------------------------


_ADMIN_CHAT = 123456789


async def _credit_record(
    application: Application, provider: str, external_id: str
) -> ProcessedWebhook | None:
    """Read the row with plain SQL, not through the repo.

    One of the cases below breaks ``ProcessedWebhooksRepo.get`` on
    purpose; an assertion helper that went through the same method
    would break with it and hide what the test is actually checking.
    """
    async with application.engines.session(DBName.ECONOMY)() as s:
        row = await s.execute(
            select(ProcessedWebhook).where(
                ProcessedWebhook.provider == provider,
                ProcessedWebhook.external_id == external_id,
            )
        )
        return row.scalar_one_or_none()


async def _rollypay_credit_then_reverse(
    tmp_path: Path,
    *,
    credit_payment_id: str = "PAY-174",
    reversal_payment_id: str = "PAY-174",
    event_type: str = "payment.refunded",
    user_id: int = 55,
    deliveries: int = 1,
) -> tuple[list[str], ProcessedWebhook | None, ProcessedWebhook | None]:
    """Credit one RollyPay payment for real, then reverse it.

    The credit half is deliberately not hand-rolled into the table:
    what #174 relies on is that the id the credit path files a row
    under is the id the reversal path arrives with. Inserting the row
    directly would test the SELECT and quietly stop testing that
    contract — the exact thing that does NOT hold for Stripe.

    Only the owner's DMs are kept: the credit half legitimately DMs the
    payer their top-up receipt, and that is noise here. The capture is
    installed BEFORE the credit rather than between the two requests —
    otherwise the receipt reaches the real Telegram client, whose
    doomed connection lingers as an unclosed socket and fails a later
    test under warnings-as-errors.

    Returns the owner's DMs, the row under ``credit_payment_id`` and
    the row under ``reversal_payment_id``. The third is normally the
    same row as the second and only diverges when the caller reverses
    an id that was never credited — the #226 case, where what matters
    is precisely what appeared under the *reversal* id.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:
        if chat_id == _ADMIN_CHAT:
            sent.append(text)

    try:
        await _seed_wallet(application, user_id, balance=100)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            paid = json.dumps(_rollypay_payload(payment_id=credit_payment_id)).encode()
            credited = c.post("/rollypay-webhook", content=paid, headers=_rollypay_headers(paid))
            assert credited.status_code == 200
            assert await _processed_count(application, "rollypay") == 1

            payload = _rollypay_payload(payment_id=reversal_payment_id)
            payload["event_type"] = event_type
            payload["status"] = "paid"
            body = json.dumps(payload).encode()
            headers = _rollypay_headers(body)
            for _ in range(deliveries):
                resp = c.post("/rollypay-webhook", content=body, headers=headers)
                assert resp.status_code == 200

        record = await _credit_record(application, "rollypay", credit_payment_id)
        reversed_row = await _credit_record(application, "rollypay", reversal_payment_id)
    finally:
        await application.close()
    return sent, record, reversed_row


async def test_a_reversal_alert_names_the_user_it_credited(tmp_path: Path) -> None:
    """The owner must not have to look the payer up by hand.

    "Списывать ли монеты" is a question about a person, and until now
    the alert answered it with an opaque provider id.
    """
    sent, _, _ = await _rollypay_credit_then_reverse(tmp_path)

    assert len(sent) == 1
    text = sent[0]
    assert "Пользователь: <code>55</code>" in text
    assert "Было начислено:" in text
    # The coins actually minted, not the provider's rouble figure — the
    # debit the owner is weighing is denominated in coins.
    assert "🪙" in text
    assert "НЕ списаны автоматически" in text


async def test_a_reversal_alert_admits_the_commissions_it_cannot_show(
    tmp_path: Path,
) -> None:
    """#1272: ``Было начислено`` is not the size of the hole.

    The same top-up minted the referral kickback and the developer cut
    on top of the buyer's coins, and a reversal claws back neither. The
    owner reads this card to decide whether to debit, and until now the
    only number on it understated the loss by up to the sum of the two
    percents with nothing saying so. The exact amounts are gone by the
    time the reversal arrives — ``last_commissions`` is per-event and
    ``CreditRecord`` has no commission columns — so the card owes the
    owner the bound instead of a silence that reads as precision.

    The tombstone half matters too: a refund that beat its payment
    credited nobody and therefore minted no commission, so the same
    line would be a lie there.
    """
    economy = _settings(tmp_path / "probe").economy
    percent = economy.referral_commission_percent + economy.developer_commission_percent
    assert percent > 0, "the fixture must configure a commission to test"

    sent, _, _ = await _rollypay_credit_then_reverse(tmp_path)

    assert len(sent) == 1
    assert f"до {percent}%" in sent[0]
    assert "Возврат их не отменяет" in sent[0]

    ghost, _, _ = await _rollypay_credit_then_reverse(
        tmp_path / "ghost", reversal_payment_id="PAY-GHOST"
    )

    assert len(ghost) == 1
    assert "Возврат их не отменяет" not in ghost[0]


async def test_a_reversal_marks_the_credit_row_it_cancels(tmp_path: Path) -> None:
    """The fact has to outlive the process.

    The counter resets on restart and the DM scrolls away; the payout
    desk needs to still see this in a week.
    """
    _, record, _ = await _rollypay_credit_then_reverse(tmp_path)

    assert record is not None
    assert record.reversed_at is not None
    assert record.reversed_event == "payment.refunded"
    # And the credit itself is untouched — no wallet, no rewriting of
    # what was originally minted.
    assert record.user_id == 55
    assert record.credited_amount > 0


async def test_a_redelivered_reversal_says_it_is_a_repeat(tmp_path: Path) -> None:
    """Twice-delivered is not twice-reversed, and the wording must say so.

    RollyPay retries callbacks. Without this line the owner reads two
    identical alerts and concludes the user charged back twice — which
    the runbook calls a scheme rather than bookkeeping.
    """
    sent, record, _ = await _rollypay_credit_then_reverse(tmp_path, deliveries=2)

    assert len(sent) == 2
    assert "уже отмечался" not in sent[0]
    assert "уже отмечался" in sent[1]
    assert "повторную доставку" in sent[1]
    assert record is not None
    assert record.reversed_event == "payment.refunded"


async def test_a_reversal_for_a_payment_we_never_credited_says_so(
    tmp_path: Path,
) -> None:
    """No row, no user — and the alert must not imply otherwise.

    A merchant-side refund of an abandoned checkout looks exactly like
    this. Inventing a user here would put the owner one tap away from
    debiting an innocent balance.
    """
    sent, record, ghost = await _rollypay_credit_then_reverse(
        tmp_path, reversal_payment_id="PAY-GHOST"
    )

    assert len(sent) == 1
    text = sent[0]
    assert "Пользователь: <i>не определён</i>" in text
    assert "начисления по этому идентификатору у бота нет" in text
    # The credited payment is a different id and stays unmarked.
    assert record is not None
    assert record.reversed_at is None
    # #226: but the ghost id does get a tombstone of its own. "We never
    # credited this" and "this must never be credited" are different
    # claims, and after this refund both are true of PAY-GHOST.
    assert ghost is not None
    assert ghost.credited_amount == 0
    assert ghost.user_id == 0
    assert ghost.reversed_event == "payment.refunded"
    # And this is the FIRST notice of it, so the card must not reach for
    # the redelivery wording just because a row now exists — the alert
    # is composed from the state as it was before the tombstone.
    assert "уже отмечался" not in text


async def test_a_refund_that_beat_its_payment_refuses_the_late_credit(
    tmp_path: Path,
) -> None:
    """#226, the whole point: order of delivery is not order of truth.

    RollyPay retries a ``paid`` callback for roughly an hour. Answer
    one of those retries with a 503 — which this bot does, on purpose,
    whenever the credit pipeline hits a locked database — then refund
    the payment inside that hour, and the refund lands first. Before
    the tombstone the surviving retry sailed through an idempotency
    gate that had nothing to check against and minted coins for money
    the merchant had already sent back.

    Two alerts are expected, not one: the refund itself, and then the
    late payment being turned away. The second is the honest half of
    the trade — a refused credit is invisible in a log full of
    duplicate deliveries, and if the refund is ever cancelled this is
    the only trace that a real buyer paid and got nothing.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:
        if chat_id == _ADMIN_CHAT:
            sent.append(text)

    before = _uncredited_count("rollypay")
    try:
        await _seed_wallet(application, 55, balance=100)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            refund = _rollypay_payload(payment_id="PAY-226")
            refund["event_type"] = "payment.refunded"
            refund["status"] = "paid"
            first = json.dumps(refund).encode()
            resp = c.post("/rollypay-webhook", content=first, headers=_rollypay_headers(first))
            assert resp.status_code == 200

            # ...and only now the payment it cancels finally gets through.
            second = json.dumps(_rollypay_payload(payment_id="PAY-226")).encode()
            resp = c.post("/rollypay-webhook", content=second, headers=_rollypay_headers(second))
            assert resp.status_code == 200

        balance = await _wallet_balance(application, 55)
        rows = await _processed_count(application, "rollypay")
        tombstone = await _credit_record(application, "rollypay", "PAY-226")
    finally:
        await application.close()

    assert balance == 100, "the late paid callback credited a refunded payment"
    assert rows == 1
    assert tombstone is not None
    assert tombstone.credited_amount == 0
    # The refusal is counted on the same series as any other verified
    # payment that did not become coins.
    assert _uncredited_count("rollypay") == before + 1
    assert len(sent) == 2, "the refused credit was swallowed silently"
    assert "уже возвращённому платежу" in sent[1]
    assert "PAY-226" in sent[1]
    assert "<code>55</code>" in sent[1]


async def test_a_stripe_reversal_writes_no_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe is excluded from the tombstone, and must stay excluded.

    Stripe credits under the checkout ``session_id`` and reverses under
    ``payment_intent`` / ``charge``. A tombstone filed under the
    reversal id would therefore bar an id that was never going to be
    credited anyway — and would sit there ready to refuse a genuinely
    unrelated future payment that happened to arrive under it.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    try:
        payload = _stripe_refund_payload(object_id="ch_226", payment_intent="pi_226")
        _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)

        async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
            return None

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            resp = c.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)
        assert resp.status_code == 200
        assert await _processed_count(application, "stripe") == 0
    finally:
        await application.close()


async def test_a_stripe_reversal_admits_the_ids_do_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe cannot resolve, and the alert says why rather than lying.

    Stripe credits under the checkout ``session_id`` and reverses under
    ``payment_intent`` / ``charge``. Reusing RollyPay's "начисления нет"
    wording here would assert something false about the bot's own
    records and send the owner away from a lookup that would succeed by
    hand in the dashboard.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    try:
        payload = _stripe_refund_payload(object_id="ch_174", payment_intent="pi_174")
        _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
            sent.append(text)

        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            resp = c.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)
    finally:
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    text = sent[0]
    assert "Пользователь: <i>не определён</i>" in text
    assert "другим идентификатором" in text
    assert "начисления по этому идентификатору у бота нет" not in text
    # The identifiers the owner searches the dashboard with survive.
    assert "pi_174" in text


async def test_the_alert_still_fires_when_the_lookup_itself_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken join must not cost the owner the whole alert.

    The lookup is new machinery added in front of a message that was
    already reliable, on a route that has already committed to 200. A
    locked database or a schema predating migration 0014 has to
    degrade to the old wording, not to silence.
    """

    async def _boom(*_: object, **__: object) -> CreditRecord | None:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(ProcessedWebhooksRepo, "get", _boom)

    before = _reversal_count("rollypay", "payment.refunded")
    sent, _, _ = await _rollypay_credit_then_reverse(tmp_path)

    assert _reversal_count("rollypay", "payment.refunded") == before + 1
    assert len(sent) == 1
    assert "возврат средств" in sent[0]
    assert "PAY-174" in sent[0]


# ---------------------------------------------------------------------------
# #188 — an unauthenticated reversal is told, never written down
#
# Every other route here proves who is calling: crypto and RollyPay by
# HMAC, Stripe by its signature scheme. The YooKassa one cannot —
# YooKassa does not sign its notifications at all, and the adapter's
# ``verify_signature`` is a credentials gate wearing the name of one.
# Since #174 that route's refund arm writes to the money database, so
# until #188 a single anonymous POST to a public URL could stamp
# ``reversed_at`` on a real credit. The damage is not the stamp: it is
# that every later alert about the *genuine* chargeback then reads
# "уже отмечался … похоже на повторную доставку", which the runbook
# tells the owner to ignore. One request, and the only chargeback
# signal the bot has says the opposite of the truth, permanently.
#
# So the refund is still announced — a missed chargeback costs more
# than a false one — and it writes nothing.
# ---------------------------------------------------------------------------


async def _yookassa_credit_then_reverse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    credit_payment_id: str = "PAY-188",
    reversal_payment_id: str = "PAY-188",
    deliveries: int = 1,
) -> tuple[list[str], ProcessedWebhook | None, ProcessedWebhook | None]:
    """Credit one YooKassa payment for real, then refund it.

    The YooKassa twin of :func:`_rollypay_credit_then_reverse`, and for
    the same reason: what is under test is that the id the credit path
    files a row under is the id the refund path arrives with. The two
    helpers differ only where the providers do — the credit half needs
    the stubbed SDK, and the refund half needs no headers at all, which
    is the whole problem.

    Only the owner's DMs are kept; the credit half legitimately sends
    the payer a receipt. The capture goes in before the credit so that
    receipt never reaches a real Telegram client and leaves an unclosed
    socket for a later test to trip over under warnings-as-errors.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:
        if chat_id == _ADMIN_CHAT:
            sent.append(text)

    try:
        _install_fake_yookassa(monkeypatch, succeeded=True)
        await _seed_wallet(application, 42, balance=100)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture  # type: ignore[method-assign]
        with TestClient(fastapi_app) as c:
            credited = c.post(
                "/yookassa-webhook",
                json=_yookassa_payload(payment_id=credit_payment_id),
            )
            assert credited.status_code == 200
            assert await _processed_count(application, "yookassa") == 1

            refund = _yookassa_refund_payload(refund_id="REF-188", payment_id=reversal_payment_id)
            for _ in range(deliveries):
                resp = c.post("/yookassa-webhook", json=refund)
                assert resp.status_code == 200

        record = await _credit_record(application, "yookassa", credit_payment_id)
        reversed_row = await _credit_record(application, "yookassa", reversal_payment_id)
    finally:
        await application.close()
    return sent, record, reversed_row


async def test_an_unsigned_yookassa_refund_leaves_the_credit_row_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert fires; the database does not move.

    Note what is *not* asserted away here: the row still holds the user
    and the coins the credit minted. Nothing about the top-up is in
    doubt — only the claim that it came back.
    """
    sent, record, _ = await _yookassa_credit_then_reverse(tmp_path, monkeypatch)

    assert len(sent) == 1, "the refund alert must still reach the owner"
    assert record is not None
    assert record.reversed_at is None
    assert record.reversed_event is None
    assert record.user_id == 42
    assert record.credited_amount > 0


async def test_an_unsigned_yookassa_refund_files_no_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #226 tombstone inherits #188's gate, and needs it more.

    Stamping a real credit on an anonymous POST poisons one alert. A
    tombstone on an anonymous POST is worse: it is a standing refusal.
    Anyone who can guess or observe a YooKassa payment id could file
    one ahead of time and make the genuine ``succeeded`` callback that
    follows land as a silent no-op — a stranger's top-up, cancelled by
    the cheapest possible request.
    """
    sent, record, ghost = await _yookassa_credit_then_reverse(
        tmp_path, monkeypatch, reversal_payment_id="PAY-GHOST-188"
    )

    assert len(sent) == 1, "the refund alert must still reach the owner"
    # Nothing was written under the id nobody authenticated...
    assert ghost is None
    # ...and the real credit is untouched, exactly as #188 requires.
    assert record is not None
    assert record.reversed_at is None
    assert record.credited_amount > 0


async def test_an_unsigned_yookassa_refund_says_it_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card has to admit what it does not know.

    It still names the payer — the lookup is a read and reads are
    harmless — because "who was this" is the question the owner opens
    the message to answer. What it must not do is present a stranger's
    POST in the same voice as a signed RollyPay callback.
    """
    sent, _, _ = await _yookassa_credit_then_reverse(tmp_path, monkeypatch)

    text = sent[0]
    assert "Событие не подтверждено" in text
    assert "не подписывает" in text
    assert "ничего не отмечено" in text
    # The read still happened, so the owner is not sent to grep a
    # dashboard for a fact the bot already has.
    assert "Пользователь: <code>42</code>" in text


async def test_a_forged_yookassa_refund_cannot_mute_the_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this whole section exists for, stated as the symptom.

    Two identical unsigned refunds arrive — think one forgery followed
    by the genuine notification. Before #188 the first stamped the row
    and the second inherited «уже отмечался … повторную доставку», the
    sentence the runbook reads as "already handled, no action". Both
    must read as first notices, because as far as this route can prove,
    both are.
    """
    sent, record, _ = await _yookassa_credit_then_reverse(tmp_path, monkeypatch, deliveries=2)

    assert len(sent) == 2
    assert all("уже отмечался" not in text for text in sent)
    assert all("повторную доставку" not in text for text in sent)
    assert record is not None
    assert record.reversed_at is None


async def test_a_flood_of_unsigned_refunds_stops_paging_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#228: the alert nobody can authenticate is the alert anyone can raise.

    YooKassa signs nothing, so each of these thirteen deliveries is a
    plain POST to a public URL — a stranger's loop is indistinguishable
    from this. Uncapped, every one costs the owner a DM plus a read of
    economy.db, and the genuine chargeback the route exists for arrives
    buried somewhere inside the wall.

    The counter is deliberately left outside the budget: rationing the
    DM must not make the flood invisible to whoever is watching
    Prometheus.
    """
    before = _reversal_count("yookassa", "refund.succeeded")

    sent, _, _ = await _yookassa_credit_then_reverse(tmp_path, monkeypatch, deliveries=13)

    assert len(sent) == 12, "the thirteenth card must be rationed away"
    assert _reversal_count("yookassa", "refund.succeeded") == before + 13


async def test_a_flood_cannot_spend_a_signed_reversal_out_of_its_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget an outsider can drain must not be the one the owner needs.

    A single shared counter would be the obvious implementation and the
    wrong one: thirteen anonymous POSTs would then buy silence on the
    next HMAC-proven chargeback, turning a nuisance into a way to hide
    real money leaving the merchant account. Only the unauthenticated
    half is rationed, so there is nothing here for a flood to consume.
    """
    await _yookassa_credit_then_reverse(tmp_path, monkeypatch, deliveries=13)

    sent, record, _ = await _rollypay_credit_then_reverse(tmp_path)

    assert len(sent) == 1, "a signed reversal is never rationed"
    assert record is not None
    assert record.reversed_at is not None


async def test_the_yookassa_reverify_runs_off_the_event_loop(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#228: the merchant-API reverify is synchronous ``requests``.

    Called inline it parks the single event loop for the SDK's whole
    timeout-and-retry budget, and everything else the bot is doing —
    commands, captchas, the other payment routes — stops with it. On a
    route that answers anonymous POSTs that is a freeze anyone can ask
    for.

    Asserted by where the call lands, not by a stopwatch: a worker
    thread has no running loop, the coroutine does. That distinction is
    the fix, and it survives however the test client arranges threads.
    """
    off_loop: list[bool] = []

    def _probe(self: object, body: bytes) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop.append(True)
        else:
            off_loop.append(False)

    monkeypatch.setattr(
        "telegram_invite_bot.services.payments.yookassa.YooKassaAdapter.parse_event",
        _probe,
    )

    resp = client.post("/yookassa-webhook", json=_yookassa_payload())

    assert resp.status_code == 200
    assert off_loop == [True]


async def test_a_hanging_yookassa_reverify_answers_without_crediting(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off-loop is not enough on its own — the wait needs a ceiling.

    An unreachable provider would otherwise hold the delivery for as
    long as the SDK's own retry budget runs, and with it a thread from a
    pool the whole process shares.

    503 on the way out (#1610). This used to be 200, on the reasoning
    that "YooKassa redelivers either way" — it does not. 200 is the
    acknowledgement that ends delivery, so answering it here threw the
    payment away. See ``test_a_hanging_reverify_asks_for_redelivery``
    below for the money half; this test owns the clock.

    The clock is the assertion here and has to be. "no credit"
    is equally true of a reverify that simply took its time, so it
    cannot tell a ceiling from the absence of one; only returning
    *before* the hang finishes can. The fake stalls for a second against
    a 50 ms ceiling, and the half-second bound sits far from both.
    """
    monkeypatch.setattr("telegram_invite_bot.webhook.payments._YOOKASSA_REVERIFY_TIMEOUT_S", 0.05)

    def _hang(self: object, body: bytes) -> None:
        time.sleep(1.0)

    monkeypatch.setattr(
        "telegram_invite_bot.services.payments.yookassa.YooKassaAdapter.parse_event",
        _hang,
    )
    await _seed_wallet(application, 42, balance=100)

    started = time.monotonic()
    resp = client.post("/yookassa-webhook", json=_yookassa_payload())
    elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert elapsed < 0.5, "the reverify was awaited to completion, not capped"
    assert await _wallet_balance(application, 42) == 100
    assert await _processed_count(application, "yookassa") == 0


async def test_a_hanging_reverify_asks_for_redelivery(
    application: Application, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1610 — the status code is the whole safety net here.

    YooKassa's documented contract: HTTP 200 acknowledges a
    notification and ends its delivery; any other code keeps it coming
    for 24 hours from the event. There is no documented way to ask for
    a notification again once acknowledged, so a 200 answered without
    crediting is a silently lost top-up — the customer paid and the
    coins never arrive.

    Asserting the exact code rather than ``!= 200`` on purpose: a 4xx
    would also be non-200 and would also earn redelivery, but it would
    misreport a fault that is ours as the caller's.
    """
    monkeypatch.setattr("telegram_invite_bot.webhook.payments._YOOKASSA_REVERIFY_TIMEOUT_S", 0.05)

    def _hang(self: object, body: bytes) -> None:
        time.sleep(1.0)

    monkeypatch.setattr(
        "telegram_invite_bot.services.payments.yookassa.YooKassaAdapter.parse_event",
        _hang,
    )
    from telegram_invite_bot.webhook.metrics import PAYMENT_CREDIT_FAILURES

    before = PAYMENT_CREDIT_FAILURES.labels(
        provider="yookassa", reason="reverify_timeout"
    )._value.get()

    resp = client.post("/yookassa-webhook", json=_yookassa_payload())

    assert resp.status_code == 503
    after = PAYMENT_CREDIT_FAILURES.labels(
        provider="yookassa", reason="reverify_timeout"
    )._value.get()
    assert after == before + 1, "a dropped delivery left no counter behind"


async def test_a_signed_rollypay_reversal_is_still_written_down(
    tmp_path: Path,
) -> None:
    """#188 must not have disarmed the providers that do prove it.

    ``stamp`` defaults to True and only the YooKassa route opts out;
    this is the guard that a later "let's be careful everywhere"
    refactor cannot quietly turn the permanent record off for the
    signed routes too. The #174 tests above assert the same fact from
    the other side — this one asserts it as a *contrast*, next to the
    YooKassa cases, where the difference is visible.
    """
    _, record, _ = await _rollypay_credit_then_reverse(tmp_path)

    assert record is not None
    assert record.reversed_at is not None


# ---------------------------------------------------------------------------
# #299 — two keys naming one currency
#
# ``rollypay.py`` reads the settlement currency out of ``currency`` OR
# ``payment_currency``. The outbound request uses the second name, the
# adapter historically read the first, and the module docstring promised
# the second while the code did the opposite — so neither name can be
# called authoritative from inside this repo. The adapter now treats
# them as alternatives and refuses only when both are present and
# disagree; below is that contract from all three sides.
# ---------------------------------------------------------------------------


async def test_payment_currency_alone_is_accepted(tmp_path: Path) -> None:
    """The name the outbound ``POST /payments`` request uses must work.

    ``rollypay_client.create_payment`` sends ``payment_currency``; a
    provider that echoes its own request field back is the ordinary
    case, and it must not land in the RUB refusal.
    """
    payload = _rollypay_payload(payment_id="PAY-PCUR")
    del payload["currency"]
    payload["payment_currency"] = "RUB"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload, seed_wallet_for=55)

    assert code == 200
    assert _uncredited_count("rollypay") == before
    # The payer's own top-up confirmation, and nothing else: no refusal
    # alert went to the owner.
    assert len(sent) == 1
    assert "Баланс пополнен" in sent[0]


async def test_both_currency_keys_agreeing_is_accepted(tmp_path: Path) -> None:
    """Carrying both names is not itself suspicious — only disagreeing is."""
    payload = _rollypay_payload(payment_id="PAY-BOTHCUR")
    payload["payment_currency"] = "RUB"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload, seed_wallet_for=55)

    assert code == 200
    assert _uncredited_count("rollypay") == before
    # The payer's own top-up confirmation, and nothing else: no refusal
    # alert went to the owner.
    assert len(sent) == 1
    assert "Баланс пополнен" in sent[0]


async def test_contradicting_currency_keys_are_refused(tmp_path: Path) -> None:
    """#299: a callback that names two different currencies is not priceable.

    Everything downstream prices ``amount`` as roubles. If one key says
    RUB and the other says USD, picking either one is a coin flip whose
    losing side mis-credits by the whole exchange rate. Refuse, alert,
    and let the owner settle it against the dashboard — the same
    treatment every other post-payment refusal in this file gets.
    """
    payload = _rollypay_payload(payment_id="PAY-CURCLASH")
    payload["payment_currency"] = "USD"

    before = _uncredited_count("rollypay")
    code, sent = await _post_rollypay_capturing_dms(tmp_path, payload, seed_wallet_for=55)

    assert code == 200
    assert _uncredited_count("rollypay") == before + 1
    # Only the owner alert — the wallet exists and was deliberately not
    # touched, so there is no top-up confirmation beside it.
    assert len(sent) == 1
    assert "PAY-CURCLASH" in sent[0]
    assert "Баланс пополнен" not in sent[0]


async def test_unsigned_crypto_callback_never_reaches_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller with no signature is refused before the token lookup.

    ``resolve_crypto_token`` opens an ``economy.db`` session and queries
    ``runtime_secrets`` — the same loop, pool and file the live credit
    path uses. Both size gates bound memory only, so a two-byte POST used
    to walk straight into that query: the cheapest way for an anonymous
    caller to aim database work at the money path. A missing header can
    never verify, so the 403 is the same verdict reached for free.
    """
    from telegram_invite_bot.webhook import payments as payments_module

    def _boom(**kwargs: object) -> object:
        raise AssertionError("token resolved before the caller was refused")

    monkeypatch.setattr(payments_module, "resolve_crypto_token", _boom)

    application = await _build_application(_settings(tmp_path, crypto=True))
    try:
        with TestClient(create_app(application)) as c:
            resp = c.post(
                "/crypto-webhook",
                content=b"{}",
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 403
    finally:
        await application.close()


@pytest.mark.parametrize(
    "signature",
    [
        "x",
        "0" * 63,
        "0" * 65,
        "z" * 64,
        "0" * 32 + " " + "0" * 31,
    ],
    ids=["one-byte", "too-short", "too-long", "not-hex", "hex-with-space"],
)
async def test_misshapen_crypto_signature_never_reaches_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signature: str
) -> None:
    """#1470 — presence of a header is not proof of anything.

    The first version of this gate only checked that the header was
    there, which one arbitrary byte satisfies: the anonymous SELECT
    against ``runtime_secrets`` was still one forged request away. The
    expected value is a ``hexdigest()``, so only 64 hex characters can
    survive ``compare_digest`` — everything here is refused for the same
    reason the full check would refuse it, minus the database.
    """
    from telegram_invite_bot.webhook import payments as payments_module

    def _boom(**kwargs: object) -> object:
        raise AssertionError("token resolved before the caller was refused")

    monkeypatch.setattr(payments_module, "resolve_crypto_token", _boom)

    application = await _build_application(_settings(tmp_path, crypto=True))
    try:
        with TestClient(create_app(application)) as c:
            resp = c.post(
                "/crypto-webhook",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "Crypto-Pay-API-Signature": signature,
                },
            )
            assert resp.status_code == 403
    finally:
        await application.close()


async def test_well_shaped_crypto_signature_still_reaches_the_token_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #1470 gate must not swallow a request that could verify.

    A cheap shape check earns its place only if it is exact. This is the
    other half of that claim: a 64-character hex string — the shape a
    real Crypto Pay callback carries — goes through to the token lookup
    and is judged by the HMAC, not by the regex.
    """
    from telegram_invite_bot.webhook import payments as payments_module

    resolved: list[bool] = []

    async def _spy(**kwargs: object) -> str:
        resolved.append(True)
        return "token-that-will-not-match"

    monkeypatch.setattr(payments_module, "resolve_crypto_token", _spy)

    application = await _build_application(_settings(tmp_path, crypto=True))
    try:
        with TestClient(create_app(application)) as c:
            resp = c.post(
                "/crypto-webhook",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "Crypto-Pay-API-Signature": "a1" * 32,
                },
            )
            # Still a 403 — the signature is well-formed but wrong. The
            # point is that the verdict was reached by the HMAC.
            assert resp.status_code == 403
            assert resolved == [True]
    finally:
        await application.close()


# ---------------------------------------------------------------------------
# #1183 — the crypto route's parser refusals were silent too
#
# The RollyPay route has told the owner about a paid-but-refused
# callback since #144. The crypto route, on the same class of body,
# answers 200 and says nothing — and on prod that route is armed: the
# token lives in ``economy.runtime_secrets``, not in ``.env``.
# ---------------------------------------------------------------------------


async def _post_crypto_capturing_dms(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    seed_wallet_for: int | None = None,
    seed_balance: int = 0,
) -> tuple[int, list[str]]:
    """POST one signed Crypto Pay callback and collect the DMs it sent.

    Same shape and same reason as :func:`_post_rollypay_capturing_dms`:
    the alert is the whole point, and the shared ``application``
    fixture has no admin chat to send it to.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    try:
        if seed_wallet_for is not None:
            await _seed_wallet(application, seed_wallet_for, balance=seed_balance)
        body = json.dumps(payload).encode()
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/crypto-webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": _crypto_sign(body),
                },
            )
    finally:
        await application.close()
    return resp.status_code, sent


async def test_a_crypto_invoice_without_a_user_id_reaches_the_owner(
    tmp_path: Path,
) -> None:
    """#1183: signature good, invoice paid, and nobody to credit.

    Crypto Pay stuffs the payer's id into ``payload.payload``. An
    invoice opened outside the bot — or one whose payload the provider
    dropped — arrives without it, ``parse_event`` refuses at the
    ``user_id and invoice_id`` guard, and the route acks 200. The money
    is in the merchant wallet and the payer has nothing.
    """
    payload = _crypto_payload(invoice_id="INV-NOUSER")
    payload["payload"]["payload"] = ""

    before = _uncredited_count("crypto")
    code, sent = await _post_crypto_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("crypto") == before + 1, "nothing counted the loss"
    assert len(sent) == 1
    # The invoice id is the one field that matches a row in the Crypto
    # Pay dashboard against a payer about to ask where their coins went.
    assert "INV-NOUSER" in sent[0]
    assert "монеты НЕ зачислены" in sent[0]
    # The journal prefix must name this route, not RollyPay's.
    assert "crypto:" in sent[0]
    assert "rollypay:" not in sent[0]


async def test_a_crypto_lifecycle_event_does_not_alarm_the_owner(
    tmp_path: Path,
) -> None:
    """Negative control: ``invoice_expired`` owes nobody anything."""
    payload = _crypto_payload(invoice_id="INV-GONE")
    payload["update_type"] = "invoice_expired"

    before = _uncredited_count("crypto")
    code, sent = await _post_crypto_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("crypto") == before
    assert sent == []


async def test_a_sub_coin_crypto_invoice_reaches_the_owner(tmp_path: Path) -> None:
    """The quietest refusal on this route: ``coins <= 0``.

    ``0.0005 USD * 900 = 0.45`` floors to zero, so the credit is
    dropped — deliberately, since a phantom coin is worse. What was
    missing is that the payer was still debited.
    """
    payload = _crypto_payload(amount="0.0005", invoice_id="INV-DUST")

    before = _uncredited_count("crypto")
    code, sent = await _post_crypto_capturing_dms(tmp_path, payload)

    assert code == 200
    assert _uncredited_count("crypto") == before + 1
    assert len(sent) == 1
    assert "INV-DUST" in sent[0]


async def test_a_crypto_credit_that_lands_does_not_alarm_the_owner(
    tmp_path: Path,
) -> None:
    """The healthy path is every successful top-up.

    An alert that fires on those is an alert the owner mutes inside a
    day, which costs the real ones their audience.
    """
    before = _uncredited_count("crypto")
    code, sent = await _post_crypto_capturing_dms(
        tmp_path, _crypto_payload(invoice_id="INV-OK"), seed_wallet_for=777
    )

    assert code == 200
    assert _uncredited_count("crypto") == before
    # Exactly the buyer's receipt — no owner alert beside it.
    assert len(sent) == 1
    assert "монеты НЕ зачислены" not in sent[0]


async def test_an_unsigned_crypto_body_cannot_summon_the_uncredited_alert(
    tmp_path: Path,
) -> None:
    """The alert stays behind the HMAC, like every other read of the body.

    Otherwise the endpoint is a "make the owner's phone buzz"
    primitive for anyone who can reach a public URL.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    before = _uncredited_count("crypto")
    try:
        payload = _crypto_payload(invoice_id="INV-FORGED")
        payload["payload"]["payload"] = ""
        body = json.dumps(payload).encode()
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/crypto-webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": "0" * 64,
                },
            )
    finally:
        await application.close()

    assert resp.status_code == 403
    assert _uncredited_count("crypto") == before
    assert sent == []


# ---------------------------------------------------------------------------
# #1184 — the users-session opens after the commit are unguarded
#
# ``_credit_event`` commits the economy transaction, then opens a
# separate users-session for the receipt DM. That open is outside every
# try/except: a locked users.db there raises AFTER the wallet write has
# landed, the route reads it as a transient pipeline fault, and answers
# 503. The provider redelivers, the idempotency row makes the retry a
# no-op, and the buyer never gets a receipt for coins they did get.
# ---------------------------------------------------------------------------


def _dm_failure_count(provider: str) -> float:
    """The provider's slot of the missed-receipt counter."""
    return float(PAYMENT_DM_FAILURES.labels(provider=provider)._value.get())


async def test_a_users_db_fault_after_the_credit_keeps_the_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1184: a locked users.db must not undo a committed top-up.

    The failure is deliberately aimed at the *session open*, not at
    ``send_message``. The sibling M-E-5 case fails the send, which the
    helper's own ``except`` arm already catches; this one fails one
    line earlier, where nothing was catching anything.
    """
    settings = _settings(tmp_path)
    application = await _build_application(settings)
    real_session = EngineRegistry.session
    armed: list[bool] = []

    def _session(self: EngineRegistry, db: DBName) -> Any:
        if armed and db is DBName.USERS:
            raise _db_locked()
        return real_session(self, db)

    monkeypatch.setattr(EngineRegistry, "session", _session)

    before_dm = _dm_failure_count("crypto")
    before_retry = _credit_failure_count("crypto", "db_unavailable")
    try:
        await _seed_wallet(application, 777, balance=100)
        body = json.dumps(_crypto_payload(invoice_id="INV-LOCKED")).encode()
        with TestClient(create_app(application)) as c:
            armed.append(True)
            resp = c.post(
                "/crypto-webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "crypto-pay-api-signature": _crypto_sign(body),
                },
            )
        armed.clear()
        balance = await _wallet_balance(application, 777)
        processed = await _processed_count(application, "crypto")
    finally:
        armed.clear()
        await application.close()

    # The credit committed before the fault, so asking for a retry is
    # asking for a redelivery the idempotency row will swallow — the
    # buyer would never see a receipt.
    assert resp.status_code == 200
    assert balance == 100 + 900
    assert processed == 1
    # A missed receipt is exactly what PAYMENT_DM_FAILURES tracks...
    assert _dm_failure_count("crypto") == before_dm + 1
    # ...and it is NOT a credit failure: the credit landed.
    assert _credit_failure_count("crypto", "db_unavailable") == before_retry


# ---------------------------------------------------------------------------
# #1527 — Stripe's own paid-but-uncredited hole
#
# The RollyPay leg above got a voice under #144. Stripe's identical
# hole stayed silent for one structural reason: its parser refuses on
# fields RollyPay does not even have (a non-USD settlement, an absent
# ``metadata.user_id``), and the route answered those with a bare 200.
#
# The fake SDK stubs ``construct_event`` to return ``event_payload``
# and ignores the posted body, so every case here MUST post the same
# dict it hands the fake: ``describes_paid_money`` reads the raw body
# on purpose (``parse_event`` pops the verified event), and a case
# posting ``b"{}"`` would exercise the empty body instead of its own.
# ---------------------------------------------------------------------------


def _stripe_paid_session(
    *,
    session_id: str = "cs_test_paid",
    currency: str = "usd",
    amount_total: int | None = 2000,
    metadata: dict[str, str] | None = None,
    event: str = "checkout.session.completed",
) -> dict[str, Any]:
    """A Checkout Session that cleared, shaped as Stripe sends it.

    Unlike :func:`_stripe_event` this carries ``object`` and
    ``currency``: both are what the credit path reads to refuse, and
    the first is what tells the alert it is looking at a session at
    all.
    """
    obj: dict[str, Any] = {
        "id": session_id,
        "object": "checkout.session",
        "currency": currency,
        "payment_status": "paid",
        "metadata": {"user_id": "55", "coins": "9000"} if metadata is None else metadata,
    }
    if amount_total is not None:
        obj["amount_total"] = amount_total
    return {"type": event, "data": {"object": obj}}


async def _post_stripe_capturing_dms(
    tmp_path: Path,
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid: bool = True,
) -> tuple[int, list[str]]:
    """POST one Stripe event and collect whatever the owner was sent."""
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    try:
        _install_fake_stripe(monkeypatch, valid=valid, event_payload=payload)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            resp = c.post("/stripe-webhook", json=payload, headers=_STRIPE_SIG_HEADERS)
    finally:
        await application.close()
    return resp.status_code, sent


async def test_a_stripe_session_settled_outside_usd_reaches_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1527: the payer paid in euros and the parser refused, silently.

    Refusing is right — ``coins_for_usd_cents`` would price euro cents
    as dollar cents — but the money is on the merchant account either
    way, and until now the only trace was a WARNING nobody reads.
    """
    payload = _stripe_paid_session(session_id="cs_eur", currency="eur")

    before = _uncredited_count("stripe")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch)

    assert code == 200
    assert _uncredited_count("stripe") == before + 1, "nothing counted the loss"
    assert len(sent) == 1
    assert "монеты НЕ зачислены" in sent[0]
    # The session id matches a dashboard row; the payer id says whom to
    # credit by hand. An alert missing either one is unactionable.
    assert "Платёж: <code>cs_eur</code>" in sent[0]
    assert "Пользователь: <code>55</code>" in sent[0]
    # Minor units divided, not printed raw: 2000 is 20 euros, and an
    # owner reading "2000 EUR" reacts to the wrong number.
    assert "20.00 EUR" in sent[0]
    assert "<code>stripe:</code>" in sent[0]


async def test_a_stripe_session_without_a_user_id_reaches_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole through the other reachable field.

    With no ``metadata.user_id`` there is nobody to credit, so the
    alert admits it rather than inventing an id — the sum and the
    session are what turn this into a dashboard lookup.
    """
    payload = _stripe_paid_session(session_id="cs_nouser", metadata={})

    before = _uncredited_count("stripe")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch)

    assert code == 200
    assert _uncredited_count("stripe") == before + 1
    assert len(sent) == 1
    assert "Платёж: <code>cs_nouser</code>" in sent[0]
    assert "Пользователь: <code>?</code>" in sent[0]
    assert "20.00 USD" in sent[0]


async def test_a_stripe_session_still_unpaid_does_not_alarm_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control, and the one that decides whether this is usable.

    A delayed-notification method commits days before it clears, and
    the credit arrives later with ``async_payment_succeeded``. An
    alert on every such session is an alert the owner mutes inside a
    week, which costs the reversal alerts their audience too.
    """
    payload = _stripe_paid_session(session_id="cs_pending")
    payload["data"]["object"]["payment_status"] = "unpaid"

    before = _uncredited_count("stripe")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch)

    assert code == 200
    assert _uncredited_count("stripe") == before
    assert sent == []


async def test_stripe_lifecycle_traffic_does_not_alarm_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe delivers hundreds of event types to this one endpoint.

    None of them is a top-up, and each one that fired an alert would
    be a reason to stop reading the alerts.
    """
    payload: dict[str, Any] = {
        "type": "payment_intent.created",
        "data": {"object": {"id": "pi_x", "object": "payment_intent", "amount": 2000}},
    }

    before = _uncredited_count("stripe")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch)

    assert code == 200
    assert _uncredited_count("stripe") == before
    assert sent == []


async def test_a_stripe_dispute_closed_still_alarms_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hazard that ruled out a RollyPay-shaped exception list.

    ``charge.dispute.closed`` sits outside ``REVERSAL_EVENTS`` on
    purpose — a dispute can close *won*. A "not a reversal, therefore
    paid money" predicate would greet that with a red alert saying
    coins are owed.
    """
    payload = _stripe_refund_payload(event="charge.dispute.closed")

    before_uncredited = _uncredited_count("stripe")
    before_reversals = _reversal_count("stripe", "charge.dispute.closed")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch)

    assert code == 200
    assert _uncredited_count("stripe") == before_uncredited
    assert _reversal_count("stripe", "charge.dispute.closed") == before_reversals
    assert sent == []


async def test_a_stripe_reversal_is_reported_once_and_as_a_reversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two alerts must not both fire on one body.

    The ordinary refund body cannot reach both branches — its
    ``data.object`` is a Charge, which ``describes_paid_money``
    refuses. What can is the shape below: a listed reversal event
    delivered with a session in ``data.object``, which satisfies
    the object-shaped predicate as well. That is what ``elif``
    settles, and a plain second ``if`` would send two alerts for
    one event — the reliable way to teach an owner to skim them.
    """
    payload = _stripe_refund_payload(object_id="ch_once")
    payload["data"]["object"].update(
        {"object": "checkout.session", "payment_status": "paid", "amount_total": 2000}
    )

    before = _uncredited_count("stripe")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch)

    assert code == 200
    assert _uncredited_count("stripe") == before
    assert len(sent) == 1
    assert "возврат средств" in sent[0]
    assert "монеты НЕ зачислены" not in sent[0]


async def test_an_unsigned_stripe_body_cannot_summon_the_uncredited_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert stays behind the HMAC, like every other read of the body.

    Otherwise the endpoint is a "make the owner's phone buzz"
    primitive for anyone who can reach a public URL — which is exactly
    why the YooKassa half of #1527 was left unbuilt.
    """
    payload = _stripe_paid_session(session_id="cs_forged", currency="eur")

    before = _uncredited_count("stripe")
    code, sent = await _post_stripe_capturing_dms(tmp_path, payload, monkeypatch, valid=False)

    assert code == 400
    assert _uncredited_count("stripe") == before
    assert sent == []


# ---------------------------------------------------------------------------
# YooKassa: reverified, refused, and now announced (#1643)
# ---------------------------------------------------------------------------


async def _post_yookassa_capturing_dms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deliveries: int = 1,
    payment_id: str = "PAY-UC",
    **fake: Any,
) -> tuple[int, list[str]]:
    """POST one YooKassa callback and collect whatever the owner was sent.

    ``**fake`` goes straight to :func:`_install_fake_yookassa`, i.e. it
    describes the *reverified* payment — the object the alert is built
    from. The posted body stays a plain valid callback throughout,
    which is the point: this route reads nothing from it but the id.

    ``deliveries`` reposts the same body, for the ration case. Every
    delivery re-enters the parser, because the refusals below never
    reach the processed-webhook table.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    try:
        _install_fake_yookassa(monkeypatch, succeeded=True, **fake)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            payload = _yookassa_payload(payment_id)
            for _ in range(deliveries):
                resp = c.post("/yookassa-webhook", json=payload)
    finally:
        await application.close()
    return resp.status_code, sent


async def test_a_yookassa_payment_with_no_user_id_reaches_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1643: the money arrived, the parser refused, and nobody was told.

    This is the shape that motivated the ticket. ЮKassa confirmed the
    payment on a second, outbound request — so the merchant account
    really did move — and the only trace was a WARNING.
    """
    before = _uncredited_count("yookassa")
    code, sent = await _post_yookassa_capturing_dms(tmp_path, monkeypatch, user_id=None)

    assert code == 200
    assert _uncredited_count("yookassa") == before + 1, "nothing counted the loss"
    assert len(sent) == 1
    assert "ЮKassa: оплата прошла, монеты НЕ зачислены" in sent[0]
    assert "Платёж: <code>PAY-UC</code>" in sent[0]
    # No user id is precisely the finding, so the card prints "?" and
    # says so in words rather than inventing a payer.
    assert "Пользователь: <code>?</code>" in sent[0]
    assert "нет user_id" in sent[0]
    assert "499.00 RUB" in sent[0]
    assert "<code>payments.yookassa:</code>" in sent[0]


async def test_the_yookassa_card_does_not_claim_a_signature_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The objection that kept this alert unbuilt through #1527 and #1583.

    YooKassa callbacks carry no HMAC. The sentence the other three
    routes earn — "подпись верна" — would be a false claim printed
    beside a real payment id, to an owner about to move real money.
    What this route can honestly say is that it asked ЮKassa.
    """
    _, sent = await _post_yookassa_capturing_dms(tmp_path, monkeypatch, user_id=None)

    assert "Подпись" not in sent[0]
    assert "подтверждён повторным запросом к API ЮKassa" in sent[0]


async def test_the_yookassa_card_names_the_gate_instead_of_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No "чаще всего" shortlist here — the parser knows which gate fired.

    The three signed routes guess from a shortlist because their
    refusal is decided inside a parser they cannot interrogate. This
    one carries the verdict in the value it returns.
    """
    _, sent = await _post_yookassa_capturing_dms(
        tmp_path, monkeypatch, currency="USD", payment_id="PAY-USD"
    )

    assert "чаще всего" not in sent[0]
    assert "расчёт не в рублях" in sent[0]
    # Here the payer IS known, and the amount is printed with the
    # currency that caused the refusal — "499.00" alone would read as
    # roubles and hide the whole finding.
    assert "Пользователь: <code>42</code>" in sent[0]
    assert "499.00 USD" in sent[0]


@pytest.mark.parametrize(
    ("fake", "verdict"),
    [
        ({"user_id": "not-a-number"}, "metadata платежа не читается"),
        ({"rub": "NaN"}, "сумма платежа не читается"),
        ({"rub": "-5.00"}, "сумма платежа не читается"),
        ({"rub": "0.01"}, "сумма меньше одной монеты"),
    ],
    ids=["bad_metadata", "unreadable_amount", "negative_amount", "below_one_coin"],
)
async def test_every_reverified_refusal_reaches_the_owner_with_its_own_words(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake: dict[str, Any],
    verdict: str,
) -> None:
    """All six post-reverify refusals, not the "three" the tickets counted.

    Each one loses the same money in a different way, and an owner who
    gets the wrong sentence acts on the wrong payment.
    """
    before = _uncredited_count("yookassa")
    code, sent = await _post_yookassa_capturing_dms(tmp_path, monkeypatch, **fake)

    assert code == 200
    assert _uncredited_count("yookassa") == before + 1
    assert len(sent) == 1
    assert verdict in sent[0]


async def test_a_flood_of_refusals_is_rationed_but_still_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This route is reachable by anyone who knows a real payment id.

    Unlike the signed three, nothing here costs the sender a secret,
    so an unbounded card is a "make the owner's phone ring" primitive.
    The counter has no such problem and keeps the true total, which is
    where a silenced flood stays visible.
    """
    from telegram_invite_bot.webhook import payments as payments_module

    before = _uncredited_count("yookassa")
    budget = payments_module._UNCREDITED_ALERT_BUDGET
    code, sent = await _post_yookassa_capturing_dms(
        tmp_path, monkeypatch, deliveries=budget + 3, user_id=None
    )

    assert code == 200
    assert len(sent) == budget
    assert _uncredited_count("yookassa") == before + budget + 3


async def test_a_body_only_refusal_still_reaches_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap stayed shut on the half that is not proof of payment.

    A callback whose id ЮKassa does not report as succeeded proves
    nothing — the body is unsigned, so a stranger can post any id at
    all. Alerting here would hand that stranger the owner's attention.
    """
    before = _uncredited_count("yookassa")
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = 123456789
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:  # noqa: ARG001
        sent.append(text)

    try:
        _install_fake_yookassa(monkeypatch, succeeded=False)
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            resp = c.post("/yookassa-webhook", json=_yookassa_payload("PAY-MISS"))
    finally:
        await application.close()

    assert resp.status_code == 200
    assert sent == []
    assert _uncredited_count("yookassa") == before


async def test_the_reversal_lookup_takes_the_writer_lock_before_it_reads(
    tmp_path: Path,
) -> None:
    """#1440: the no-op UPDATE is the first statement, the read second.

    ``db/engines.py`` opens transactions lazily — it issues ``BEGIN
    IMMEDIATE`` only when it sees a write-headed statement, and
    ``"select"`` is not one. An unguarded lookup would therefore read
    outside any transaction, and a concurrent ``paid`` delivery could
    commit its credit row between that read and the tombstone INSERT
    that follows. The order asserted here is the whole remedy.

    ``BEGIN IMMEDIATE`` is issued through the raw DBAPI cursor and
    never reaches this listener, so the UPDATE is what stands in for
    it: seeing it first means the lock was taken first.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    sent: list[str] = []
    heads: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:
        if chat_id == _ADMIN_CHAT:
            sent.append(text)

    def _record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "processed_webhooks" in statement.lower():
            heads.append(statement.split(None, 1)[0].upper())

    engine = application.engines.engine(DBName.ECONOMY).sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        payload = _rollypay_payload(payment_id="PAY-1440-LOCK")
        payload["event_type"] = "payment.refunded"
        payload["status"] = "paid"
        body = json.dumps(payload).encode()
        with TestClient(fastapi_app) as c:
            resp = c.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    assert heads[0] == "UPDATE"
    assert "SELECT" in heads[1:]


async def test_an_unsigned_yookassa_refund_takes_no_writer_lock(
    tmp_path: Path,
) -> None:
    """#188 outranks #1440 on the one route nobody authenticates.

    The lock is gated on ``stamp`` on purpose. A ``/yookassa-webhook``
    body carries no signature, so letting it take the economy
    database's writer lock would hand any stranger a way to make every
    other economy writer queue behind a free request. Reading without
    the lock is harmless there because nothing is written afterwards.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    sent: list[str] = []
    heads: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:
        if chat_id == _ADMIN_CHAT:
            sent.append(text)

    def _record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "processed_webhooks" in statement.lower():
            heads.append(statement.split(None, 1)[0].upper())

    engine = application.engines.engine(DBName.ECONOMY).sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        with TestClient(fastapi_app) as c:
            resp = c.post(
                "/yookassa-webhook",
                json=_yookassa_refund_payload(payment_id="PAY-1440-FREE"),
            )
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    assert "UPDATE" not in heads
    assert heads and set(heads) == {"SELECT"}


async def test_a_collided_reversal_stamp_still_alerts_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1440: the alert outranks the lookup, collision included.

    With the writer lock in place an ``IntegrityError`` on the stamp
    should be unreachable — but if a second writer ever reaches this
    table by a route that does not take the lock, the owner must still
    hear about the chargeback. The branch is proven by its wording:
    a stamp that succeeded would file a #226 tombstone and the card
    would say the refund overtook its payment, whereas a stamp that
    collided resolves to nothing and the card says so honestly.

    The ERROR record is asserted too, and that half is what pins the
    dedicated ``except IntegrityError`` branch: the broad ``except``
    below it would swallow the same exception just as quietly, only at
    WARNING and under a message that reads like a routine lookup miss.
    A lost ``reversed_at`` is not routine.
    """
    settings = _settings(tmp_path)
    settings.bot.admin_chat_id = _ADMIN_CHAT
    application = await _build_application(settings)
    sent: list[str] = []

    async def _capture(chat_id: int, text: str, **_: object) -> None:
        if chat_id == _ADMIN_CHAT:
            sent.append(text)

    async def _collide(*_: object, **__: object) -> None:
        raise IntegrityError(
            "INSERT INTO processed_webhooks",
            (),
            Exception("UNIQUE constraint failed"),
        )

    records: list[str] = []
    handler_id = logger.add(records.append, level="ERROR", format="{message}")
    monkeypatch.setattr(ProcessedWebhooksRepo, "mark_reversed", _collide)
    try:
        fastapi_app = create_app(application)
        fastapi_app.state.application.bot.send_message = _capture
        payload = _rollypay_payload(payment_id="PAY-1440-RACE")
        payload["event_type"] = "payment.refunded"
        payload["status"] = "paid"
        body = json.dumps(payload).encode()
        with TestClient(fastapi_app) as c:
            resp = c.post("/rollypay-webhook", content=body, headers=_rollypay_headers(body))
    finally:
        logger.remove(handler_id)
        await application.close()

    assert resp.status_code == 200
    assert len(sent) == 1
    assert any("reversal credit stamp collided" in r for r in records)
    assert "Скорее всего монеты и не выдавались" in sent[0]
    assert "возврат опередил сам платёж" not in sent[0]
