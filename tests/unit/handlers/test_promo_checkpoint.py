"""#1870: ``/promo`` and ``/promo_create`` commit before they answer.

Both handlers run their whole write inside ``PromoService`` and then
speak to Telegram unwrapped. ``BaseSessionMiddleware`` only commits
after the handler returns (``middlewares/base.py:124-125``), so a
rejected reply used to roll the write back:

* ``/promo`` un-redeems a code the user was never told they had spent
  — self-healing, but they are left staring at nothing;
* ``/promo_create`` throws the minted row away while the developer is
  told the mint failed.

Worse than either, ``PromoRepo.reserve_use`` is a guarded ``UPDATE``,
so it takes ``BEGIN IMMEDIATE`` on ``economy.db`` even when it matches
no row — the refusal branches hold the single writer slot across their
own round-trip too (``db/pragma.py:63``, 5 s ``busy_timeout``). That is
why the checkpoint sits right after the service call rather than inside
the OK branch, and why the refusal tests below matter as much as the
happy ones.

Recording stubs, not a real session: what is under test is the ORDER of
``checkpoint()`` against ``message.reply``, which a fake records exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from aiogram.filters import CommandObject
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.handlers.promo import handle_promo, handle_promo_create
from telegram_invite_bot.services.promo_service import (
    CreateOutcome,
    CreateResult,
    RedeemOutcome,
    RedeemResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.promo_service import PromoService

_DEV_ID = 123456789
_USER_ID = 555


class _FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.is_bot = False


class _FakeMessage:
    """Records replies instead of calling the Bot API."""

    def __init__(self, uid: int) -> None:
        self.chat = type("_Chat", (), {"id": uid, "type": "private"})()
        self.from_user = _FakeUser(uid)
        self.message_id = 7
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _RedeemStub:
    def __init__(self, outcome: RedeemOutcome) -> None:
        self._outcome = outcome

    async def redeem(self, **_kwargs: Any) -> RedeemResult:  # noqa: ANN401
        return RedeemResult(outcome=self._outcome, reward_coins=100, new_balance=100, code="SUMMER")


class _CreateStub:
    async def create_code(self, **kwargs: Any) -> CreateResult:  # noqa: ANN401
        return CreateResult(
            outcome=CreateOutcome.OK,
            code=kwargs["code"],
            reward_coins=kwargs["reward_coins"],
            max_uses=kwargs["max_uses"],
            per_user_once=kwargs["per_user_once"],
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_DEV_ID),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


async def _redeem(outcome: RedeemOutcome) -> tuple[_FakeMessage, list[int]]:
    message = _FakeMessage(_USER_ID)
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    await handle_promo(
        cast("Message", message),
        CommandObject(prefix="/", command="promo", args="SUMMER"),
        cast("PromoService", _RedeemStub(outcome)),
        "ru",
        cast("Checkpoint", checkpoint),
    )
    return message, fired


async def test_redeem_commits_before_the_confirmation() -> None:
    """OK: three writes have landed and the reply is not wrapped."""
    message, fired = await _redeem(RedeemOutcome.OK)
    assert fired == [0]
    assert len(message.replies) == 1


async def test_redeem_commits_on_an_exhausted_code_too() -> None:
    """EXHAUSTED is a 0-row ``UPDATE`` — it still holds the writer slot.

    Move the ``await checkpoint()`` inside the OK branch and this test
    fails while the one above still passes.
    """
    message, fired = await _redeem(RedeemOutcome.EXHAUSTED)
    assert fired == [0]
    assert len(message.replies) == 1


async def test_the_help_blurb_never_reaches_the_service() -> None:
    """No code typed → no write, so no checkpoint either."""
    message = _FakeMessage(_USER_ID)
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    await handle_promo(
        cast("Message", message),
        CommandObject(prefix="/", command="promo", args=None),
        cast("PromoService", _RedeemStub(RedeemOutcome.OK)),
        "ru",
        cast("Checkpoint", checkpoint),
    )
    assert fired == []
    assert len(message.replies) == 1


async def test_the_mint_commits_before_the_developer_card(tmp_path: Path) -> None:
    """``/promo_create``: the row is durable before the card goes out."""
    message = _FakeMessage(_DEV_ID)
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    await handle_promo_create(
        cast("Message", message),
        CommandObject(prefix="/", command="promo_create", args="SUMMER 100 5"),
        cast("PromoService", _CreateStub()),
        _settings(tmp_path),
        "ru",
        cast("Checkpoint", checkpoint),
    )
    assert fired == [0]
    assert len(message.replies) == 1


async def test_a_refused_mint_never_reaches_the_checkpoint(tmp_path: Path) -> None:
    """Both usage refusals are decided before the INSERT and take no
    lock, so there is nothing to commit and the handler must not try."""
    message = _FakeMessage(_DEV_ID)
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    await handle_promo_create(
        cast("Message", message),
        CommandObject(prefix="/", command="promo_create", args="SUMMER"),
        cast("PromoService", _CreateStub()),
        _settings(tmp_path),
        "ru",
        cast("Checkpoint", checkpoint),
    )
    assert fired == []
    assert len(message.replies) == 1
