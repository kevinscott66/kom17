"""Argument parsing for ``/promo_create`` (#745).

The command's syntax is ``<CODE> <reward_coins> [max_uses] [once=…]``.
Splitting positionals from extras on "does the token contain an ``=``"
was wrong in both directions: the code field is free-form (
:meth:`PromoService.create_code` validates length and emptiness, not the
charset), so a code containing ``=`` was swallowed as an unknown extra
and never reached the mint; and a misspelt extra like ``onse=false`` was
swallowed the same way, minting a code with the opposite ``once``
setting and telling the developer nothing.

These drive the handler directly with a recording stub: what matters is
the kwargs that reach ``create_code``, not the rendered card.
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
from telegram_invite_bot.handlers.promo import handle_promo_create
from telegram_invite_bot.services.promo_service import CreateOutcome, CreateResult

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram.types import Message

    from telegram_invite_bot.services.promo_service import PromoService

_DEV_ID = 123456789


class _FakeUser:
    id = _DEV_ID
    is_bot = False


class _FakeChat:
    id = _DEV_ID
    type = "private"


class _FakeMessage:
    """Records replies instead of calling the Bot API."""

    chat = _FakeChat()
    from_user = _FakeUser()
    message_id = 7

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _StubService:
    """Records the mint kwargs and echoes them back as a result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_code(self, **kwargs: Any) -> CreateResult:  # noqa: ANN401
        self.calls.append(kwargs)
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


async def _run(tmp_path: Path, args: str) -> tuple[_FakeMessage, _StubService]:
    message = _FakeMessage()
    service = _StubService()
    await handle_promo_create(
        cast("Message", message),
        CommandObject(prefix="/", command="promo_create", args=args),
        cast("PromoService", service),
        _settings(tmp_path),
        "ru",
    )
    return message, service


async def test_the_ordinary_form_still_mints(tmp_path: Path) -> None:
    """The happy path the extras split exists to serve."""
    _, service = await _run(tmp_path, "SUMMER 100 5 once=false")
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["code"] == "SUMMER"
    assert call["reward_coins"] == 100
    assert call["max_uses"] == 5
    assert call["per_user_once"] is False


async def test_once_defaults_to_true(tmp_path: Path) -> None:
    """Only an explicit ``false`` turns the per-user constraint off."""
    _, service = await _run(tmp_path, "AUTUMN 50")
    assert service.calls[0]["per_user_once"] is True
    # T-019: and a code with no ``max_uses`` is single-redeem, not free.
    assert service.calls[0]["max_uses"] == 1


async def test_a_code_containing_an_equals_sign_is_not_eaten(tmp_path: Path) -> None:
    """``create_code`` validates length and emptiness, not the charset —
    so ``A=B`` is a legal code and must reach the mint as one.

    Before #745 it was parsed as the extra ``a=b``, leaving one
    positional, and the developer got the usage text for a command that
    was in fact well-formed.
    """
    _, service = await _run(tmp_path, "A=B 100")
    assert service.calls[0]["code"] == "A=B"
    assert service.calls[0]["reward_coins"] == 100


async def test_a_misspelt_extra_is_refused_not_ignored(tmp_path: Path) -> None:
    """``onse=false`` used to vanish and the code was minted per-user-once
    anyway — the developer's intent inverted, in silence."""
    message, service = await _run(tmp_path, "WINTER 100 5 onse=false")
    assert service.calls == []
    assert len(message.replies) == 1


async def test_a_misspelt_extra_without_max_uses_is_refused_too(tmp_path: Path) -> None:
    """The same typo one argument earlier: it lands where ``max_uses``
    is read, so it must not parse as one either."""
    message, service = await _run(tmp_path, "WINTER 100 onse=false")
    assert service.calls == []
    assert len(message.replies) == 1


async def test_a_non_developer_never_reaches_the_parser(tmp_path: Path) -> None:
    """The dev gate is unchanged and still comes first."""
    message = _FakeMessage()
    message.from_user = cast("_FakeUser", type("_Other", (), {"id": 1, "is_bot": False})())
    service = _StubService()
    await handle_promo_create(
        cast("Message", message),
        CommandObject(prefix="/", command="promo_create", args="SUMMER 100"),
        cast("PromoService", service),
        _settings(tmp_path),
        "ru",
    )
    assert service.calls == []
    assert len(message.replies) == 1
