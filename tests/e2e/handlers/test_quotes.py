"""End-to-end ``/quote`` (CMD-1, RR-6 #71) — twin of ``test_jokes.py``.

What's worth proving:

* Each alias routes in private chat and produces a non-empty BARE reply
  (no header — legacy's text had none).
* RU users see a RU-pool entry; ``en`` users see an EN-pool entry with
  NO Cyrillic — the language switch is keyed off ``users.language``,
  which the ``SessionMiddleware`` populates via ``UserService.touch``.
* ``/quote`` answers in GROUPS (RR-6 #71). The old private-only gate
  cited legacy's ``require_group_feature("ai")``, which cannot deny — see
  the handler docstring.
* With no DeepSeek key the body comes from the local pool — fixed RNG
  pins the exact pick. With a key, the AI answer wins and Markdown
  markers are promoted to HTML.
* Quota exhaustion degrades to the pool SILENTLY: a content command that
  answers beats one that refuses.
"""

from __future__ import annotations

import html
import random
import re
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from sqlalchemy import update

from telegram_invite_bot.config.settings import AiConfig, AiQuotaSettings
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.users import User as DBUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.quotes import _PICKER, _QUOTES_EN, _QUOTES_RU
from telegram_invite_bot.services.ai_service import AiService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture(autouse=True)
def _reset_quote_picker() -> None:
    """The anti-repeat picker is a module-level singleton, so one test's
    pick narrows the next test's candidate list and the fixed-RNG
    assertions below would start depending on test order.
    """
    _PICKER.clear()


def _update(
    text: str,
    *,
    chat_type: str = "private",
    user_id: int = 444,
    language_code: str | None = "ru",
) -> Update:
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        language_code=language_code,
    )


@pytest.mark.parametrize("alias", ["/quote", "/цитата", "/kom_quote"])
async def test_quote_aliases_route_in_private(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert body == f"💭 <i>{html.escape(_QUOTES_RU[0])}</i>"


async def test_quote_uses_english_pool_for_en_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``en`` user gets an English-pool entry that contains NO Cyrillic."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/quote", language_code="en"))
    body = sent[0]["text"]
    assert body == f"💭 <i>{html.escape(_QUOTES_EN[0])}</i>"
    assert not _CYRILLIC.search(body)


def test_quote_en_pool_has_no_cyrillic() -> None:
    """Every English quote must be free of Cyrillic — guards against a
    copy-paste-from-RU regression in the pool itself.
    """
    offenders = [q for q in _QUOTES_EN if _CYRILLIC.search(q)]
    assert not offenders, f"EN quote pool has Cyrillic: {offenders}"


async def test_quote_answers_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #71: group ``/quote`` answers.

    The private-only gate this router used to carry cited legacy's
    ``require_group_feature(message, "ai", ...)``, but that call can never
    deny: ``is_feature_enabled_for_chat`` returns ``True`` in ``full``
    mode and ``feature == "ai"`` in ``restricted`` mode, and no third mode
    is ever written. The group half of the command was withheld to
    protect a no-op. ``/cmdcfg`` is the real per-group off-switch.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    result = await dispatcher.feed_update(
        bot, _update("/quote", chat_type="supergroup", user_id=555)
    )
    assert result is not UNHANDLED
    assert sent[0]["text"] == f"💭 <i>{html.escape(_QUOTES_RU[0])}</i>"


async def test_quote_prefers_ai_answer_when_configured(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a key configured the model's line wins over the pool, and its
    Markdown markers are promoted to HTML (raw ``**`` would otherwise leak
    literally under the bot-wide HTML parse mode).
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        ai_config=AiConfig(DEEPSEEK_API_KEY="test-key"),
    )
    sent = capture_outgoing(bot)

    async def _fake(
        self: AiService, prompt: str, *, system_prompt: str, max_tokens: int | None = None
    ) -> str:
        return "**Знание** — сила. (Бэкон)"

    monkeypatch.setattr(AiService, "complete_or_none", _fake)

    await dispatcher.feed_update(bot, _update("/quote"))
    assert sent[0]["text"] == "💭 <i><b>Знание</b> — сила. (Бэкон)</i>"


async def test_quote_does_not_hold_the_write_lock_across_the_model_call(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quota slot is spent before the model call, and both writes
    live on users.db.

    Under ``BEGIN IMMEDIATE`` that transaction owns the DB until the
    middleware commits — i.e. across the whole DeepSeek round-trip —
    and everyone else's update fails with ``database is locked`` after
    ``busy_timeout``. The probe writes from a separate session at the
    exact moment the model would be thinking.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        ai_config=AiConfig(DEEPSEEK_API_KEY="test-key"),
    )
    sent = capture_outgoing(bot)
    other_updates_could_write: list[bool] = []

    async def _fake(
        self: AiService, prompt: str, *, system_prompt: str, max_tokens: int | None = None
    ) -> str:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(
                update(DBUser).where(DBUser.user_id == 444).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return "Знание — сила. (Бэкон)"

    monkeypatch.setattr(AiService, "complete_or_none", _fake)

    await dispatcher.feed_update(bot, _update("/quote"))

    assert other_updates_could_write == [True]
    assert sent[0]["text"] == "💭 <i>Знание — сила. (Бэкон)</i>"


async def test_quote_falls_back_to_pool_when_ai_fails(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream failure must never surface as "❌" on a content
    command — the pool is always there.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        ai_config=AiConfig(DEEPSEEK_API_KEY="test-key"),
    )
    sent = capture_outgoing(bot)

    async def _fake(
        self: AiService, prompt: str, *, system_prompt: str, max_tokens: int | None = None
    ) -> None:
        return None

    monkeypatch.setattr(AiService, "complete_or_none", _fake)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/quote"))
    assert sent[0]["text"] == f"💭 <i>{html.escape(_QUOTES_RU[0])}</i>"


async def test_quote_rejects_implausible_ai_answer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that ignores "one short line" and writes an essay gets
    dropped rather than truncated mid-sentence.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        ai_config=AiConfig(DEEPSEEK_API_KEY="test-key"),
    )
    sent = capture_outgoing(bot)

    async def _fake(
        self: AiService, prompt: str, *, system_prompt: str, max_tokens: int | None = None
    ) -> str:
        return "x" * 5000

    monkeypatch.setattr(AiService, "complete_or_none", _fake)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/quote"))
    assert sent[0]["text"] == f"💭 <i>{html.escape(_QUOTES_RU[0])}</i>"


async def test_quote_degrades_to_pool_when_quota_exhausted(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero free ceiling means the very first call is already over
    budget. The user still gets a quote — silently, with no refusal card
    (that is ``/ask``'s job, where the user asked a real question).
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase, UsersBase],
        session_middleware=True,
        ai_config=AiConfig(DEEPSEEK_API_KEY="test-key"),
        ai_quota=AiQuotaSettings(AI_QUOTA_FREE_DAILY_LIMIT=1),
    )
    sent = capture_outgoing(bot)
    calls = 0

    async def _fake(
        self: AiService, prompt: str, *, system_prompt: str, max_tokens: int | None = None
    ) -> str:
        nonlocal calls
        calls += 1
        return "Одна строка."

    monkeypatch.setattr(AiService, "complete_or_none", _fake)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/quote"))
    await dispatcher.feed_update(bot, _update("/quote"))

    assert calls == 1, "the second call must not reach the upstream"
    assert sent[0]["text"] == "💭 <i>Одна строка.</i>"
    assert sent[1]["text"] == f"💭 <i>{html.escape(_QUOTES_RU[0])}</i>"
