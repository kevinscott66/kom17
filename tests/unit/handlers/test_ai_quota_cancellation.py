"""#1965: a deploy restart mid-``/ask`` used to charge the user twice.

The quota slot is consumed and *committed* before the upstream call —
deliberately, and ``handlers/ai.py`` spells out why: a slot spent on a
call that then failed stays spent, or a broken provider turns the daily
ceiling into no ceiling at all.

Cancellation is not that case. ``DEEPSEEK_TIMEOUT_SECONDS`` defaults to
60 and uvicorn's ``timeout_graceful_shutdown`` is 20
(``runner/webhook.py``), so a redeploy while DeepSeek is thinking
cancels the request instead of letting it finish. ``CancelledError`` is
a ``BaseException``: the handler's own ``except`` clauses, the session
middleware's rollback, ``handlers/errors.py`` and the webhook route all
match ``Exception`` and none of them see it. No response is written, so
Telegram redelivers under its at-least-once contract — and the fresh
process builds ``seen_updates`` inside ``create_app``, so the retry is
not recognised as a duplicate. Two slots, zero answers.

``services/tts_service.py`` fixed the same shape for the voice hold in
#1954 and is the reference implementation, down to the best-effort
release under ``contextlib.suppress`` followed by a bare ``raise``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import AiConfig
from telegram_invite_bot.handlers import ai as ai_mod

_UID = 4242


class _FakeMessage:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=_UID)
        self.chat = SimpleNamespace(id=_UID, type="private")
        self.reply_to_message = None
        self.message_id = 11
        self.answers: list[str] = []

    async def answer(self, text: str, **_kw: object) -> None:
        self.answers.append(text)


class _QuotaRepoStub:
    """Counts both directions so the test can assert the net effect."""

    def __init__(self) -> None:
        self.increments = 0
        self.releases = 0

    async def get_and_increment(self, _uid: int, *, now: Any = None) -> int:  # noqa: ANN401
        self.increments += 1
        return self.increments

    async def release(self, _uid: int, *, now: Any = None) -> bool:  # noqa: ANN401
        self.releases += 1
        return True


class _VipRepoStub:
    async def get_active_profile(self, _uid: int, *, now: Any = None) -> None:  # noqa: ANN401
        return None


class _CancellingService:
    """Stands in for ``AiService``: the redeploy lands mid-call."""

    def __init__(self, *_a: object, **_kw: object) -> None:
        pass

    async def ask_with_context(self, *_a: object, **_kw: object) -> str:
        raise asyncio.CancelledError


def _quota_settings(free: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        free_daily_limit=free,
        vip_daily_limit=50,
        parsed_dev_user_ids=frozenset,
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    quota_settings: SimpleNamespace | None = None,
) -> tuple[_FakeMessage, _QuotaRepoStub, list[int]]:
    monkeypatch.setattr(ai_mod, "AiService", _CancellingService)
    message = _FakeMessage()
    repo = _QuotaRepoStub()
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(repo.increments - repo.releases)

    with pytest.raises(asyncio.CancelledError):
        await ai_mod._answer_with_ai(  # noqa: SLF001
            message,  # type: ignore[arg-type]
            "привет",
            "ru",
            AiConfig(DEEPSEEK_API_KEY=SecretStr("sk-test")),
            repo,  # type: ignore[arg-type]
            _VipRepoStub(),  # type: ignore[arg-type]
            quota_settings or _quota_settings(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,
            checkpoint,  # type: ignore[arg-type]
        )
    return message, repo, fired


async def test_a_cancelled_call_hands_the_slot_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One consumed, one released — the redelivered update starts even.

    Without the release the counter keeps the slot, and the retry that
    Telegram is about to send spends a second one for the same prompt.
    """
    message, repo, _fired = await _run(monkeypatch)
    assert repo.increments == 1
    assert repo.releases == 1
    assert message.answers == []


async def test_the_release_is_committed_not_left_in_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CancelledError`` skips the middleware's commit AND its rollback.

    The middleware matches ``Exception``; a ``BaseException`` walks past
    both arms, so an uncommitted release is simply lost with the
    session. The checkpoint after the release is what makes it real —
    and the balance it observes must be zero net slots.
    """
    _message, _repo, fired = await _run(monkeypatch)
    # Two checkpoints fire on this path: the one before the upstream
    # call (net 1 — the slot is spent) and the one after the release
    # (net 0 — it is handed back).
    assert fired == [1, 0]


async def test_an_unlimited_tier_releases_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ceiling of ``0`` consumes no slot, so there is none to return.

    ``check_and_consume`` short-circuits before any DB write and reports
    ``count == 0``; releasing on that would decrement a row this request
    never touched.
    """
    _message, repo, _fired = await _run(monkeypatch, quota_settings=_quota_settings(free=0))
    assert repo.increments == 0
    assert repo.releases == 0
