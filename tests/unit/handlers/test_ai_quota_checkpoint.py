"""#1873: the AI quota refusal commits the slot it just spent.

``AiQuotaService.check_and_consume`` increments first and compares
afterwards (``ai_quota_service.py:163``), so a rejected request has
already written to ``users.db``. That is deliberate — it is what stops
a retry storm from walking past the daily ceiling.

The refusal then returns early, ABOVE the checkpoint that guards the
upstream call. Without a commit of its own, a refusal the user never
received (blocked bot, FloodWait, a group the bot was kicked from) is
rolled back by ``BaseSessionMiddleware`` together with the increment,
handing the slot straight back. Whoever can make the answer fail gets
an unlimited quota; the honest user, whose refusal arrives, does not.

The order of ``checkpoint()`` against ``message.answer`` is the whole
contract, so the stubs below record exactly that.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from telegram_invite_bot.config.settings import AiConfig
from telegram_invite_bot.handlers import ai as ai_mod

_UID = 4242


class _FakeMessage:
    """Records ``answer`` calls instead of talking to Telegram."""

    def __init__(self, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=_UID)
        self.chat = SimpleNamespace(id=_UID, type=chat_type)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kw: object) -> None:
        self.answers.append(text)


class _QuotaRepoStub:
    """One slot over the ceiling, and it records that it wrote."""

    def __init__(self) -> None:
        self.increments = 0

    async def get_and_increment(self, _uid: int, *, now: Any = None) -> int:  # noqa: ANN401
        self.increments += 1
        return 6


class _VipRepoStub:
    async def get_active_profile(self, _uid: int, *, now: Any = None) -> None:  # noqa: ANN401
        return None


def _quota_settings() -> SimpleNamespace:
    return SimpleNamespace(
        free_daily_limit=5,
        vip_daily_limit=50,
        parsed_dev_user_ids=frozenset,
    )


async def _run(chat_type: str) -> tuple[_FakeMessage, _QuotaRepoStub, list[int]]:
    message = _FakeMessage(chat_type)
    repo = _QuotaRepoStub()
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.answers))

    await ai_mod._answer_with_ai(  # noqa: SLF001
        message,  # type: ignore[arg-type]
        "привет",
        "ru",
        AiConfig(DEEPSEEK_API_KEY=SecretStr("sk-test")),
        repo,  # type: ignore[arg-type]
        _VipRepoStub(),  # type: ignore[arg-type]
        _quota_settings(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,
        checkpoint,  # type: ignore[arg-type]
    )
    return message, repo, fired


async def test_the_refusal_commits_the_slot_it_spent() -> None:
    """The increment landed, so it must survive an undeliverable card.

    Drop the checkpoint from the EXCEEDED branch and ``fired`` is ``[]``
    — the slot goes back on rollback and the ceiling stops being one.
    """
    message, repo, fired = await _run("private")
    assert repo.increments == 1
    assert fired == [0]
    assert len(message.answers) == 1


async def test_the_group_refusal_commits_too() -> None:
    """Same branch, plain-text arm: the markup differs, the debt does not."""
    message, repo, fired = await _run("supergroup")
    assert repo.increments == 1
    assert fired == [0]
    assert len(message.answers) == 1
