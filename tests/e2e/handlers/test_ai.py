"""End-to-end ``/ask`` + ``/ai`` + ``/gpt`` + ``/chat`` routing.

We monkey-patch :meth:`AiService.ask` / the OpenAI SDK boundary
rather than stand up the full HTTP chain here — the services are
unit-tested on their own, so the e2e contract is just "router →
handler → service called, reply rendered, wallet debited". Mocking
at the service boundary keeps each test focused on a single layer.

T-021 (OpenAI ``/ai`` / ``/gpt`` / ``/chat``) tests build a fake
``AsyncOpenAI`` client (``unittest.mock.AsyncMock`` shaped as
``client.chat.completions.create``) and pass it via the handler's
late-import seam (``_build_openai_client``). Never hits the network.
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AiConfig,
    AiQuotaSettings,
    OpenAiConfig,
)
from telegram_invite_bot.db.models.ai_quota import AiDailyRequest  # noqa: F401
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.handlers.ai import (
    _MEMORY_STORE,
    _RESPONSE_CACHE,
    extract_ai_direct_question,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.services import ai_service as ai_service_module
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


# #419: ``/ai`` now refuses BEFORE the quota gate when no provider key is
# set, so any test that exercises the model path has to model a
# CONFIGURED bot. The value is a dummy and nothing leaves the process:
# every one of these tests patches the ``AiService`` seam. The suite-wide
# default stays key-less (conftest.py:381) because ``/quote`` reaches the
# network directly once a key exists.
_CONFIGURED_AI = AiConfig(DEEPSEEK_API_KEY=SecretStr("test-key"))


def _update(
    text: str,
    *,
    chat_type: str = "private",
    user_id: int = 555,
    language_code: str | None = None,
    as_caption: bool = False,
) -> Update:
    """File-local defaults: user 555. Delegates to the shared builder.

    ``LanguageMiddleware`` memoises the resolved locale per user id, so
    an English case must pass its own ``user_id`` — reusing 555 after a
    Russian call would serve the Russian card twice.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        language_code=language_code,
        as_caption=as_caption,
    )


async def test_ai_no_args_renders_help(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/ai"))
    assert result is not UNHANDLED
    assert "ИИ-помощник" in sent[0]["text"]
    assert "/ask" in sent[0]["text"]


async def test_ask_without_prompt_warns(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/ask"))
    assert result is not UNHANDLED
    assert "Напиши вопрос после /ask" in sent[0]["text"]


async def test_ai_help_answers_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The help card was a Russian literal — the one screen a new
    English-speaking user hits first when they try the assistant."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/ai", user_id=601, language_code="en"))
    body = sent[0]["text"]
    assert "AI assistant" in body
    assert "/ask" in body
    assert not any("\u0400" <= ch <= "\u04ff" for ch in body), body


async def test_ask_usage_answers_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/ask", user_id=602, language_code="en"))
    body = sent[0]["text"]
    assert "Type the question after /ask" in body
    assert not any("\u0400" <= ch <= "\u04ff" for ch in body), body


async def test_ask_with_prompt_calls_service_and_replies(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    captured_prompts: list[str] = []

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        captured_prompts.append(prompt)
        return "<b>not</b> escaped"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("/ask Какая погода?"))
    assert result is not UNHANDLED
    assert captured_prompts == ["Какая погода?"]
    # HTML in the AI's answer must be escaped — parse_mode=HTML would
    # otherwise execute it.
    assert sent[0]["text"] == "&lt;b&gt;not&lt;/b&gt; escaped"


async def test_ask_in_group_routes_through_deepseek(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group ``/ask <prompt>`` now answers (legacy had no chat gate).
    The chat-context injection is still unported, so the answer just
    omits surrounding-message context — but it must not silently
    no-op now that the fallback bridge is gone."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "group ask ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("/ask hi there", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "group ask ok" in sent[0]["text"]


async def test_ask_renders_not_configured_when_api_key_missing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler must catch ``AiNotConfiguredError`` and render the
    user-facing "не настроен" copy — NOT propagate the exception, which
    would 500 the webhook and Telegram would retry indefinitely.

    Since #419 the common case never reaches this clause: a missing key
    is refused at the top of ``_answer_with_ai``, before the quota gate.
    What is left here is the race — the key cleared between that guard
    and the call — so the bot is wired WITH a key and the seam raises
    anyway, which is the only way to reach the clause at all now.

    The branch is one of the handler's few exception clauses and was
    the last uncovered piece of ai.py. Without coverage, a refactor
    that swapped the narrow except for a broader one would silently
    eat *real* AI service errors as "not configured" — exactly the
    misleading-error-message class of bug.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        raise ai_service_module.AiNotConfiguredError("DEEPSEEK_API_KEY is not set")

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("/ask тест"))
    assert result is not UNHANDLED
    assert "не настроен" in sent[0]["text"]


async def test_not_configured_is_reported_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The misconfiguration notice is the one line that has to survive a
    broken deploy; an unreadable one costs the operator a support round
    trip on top of the outage. Same race-only wiring as the test above
    (#419)."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        raise ai_service_module.AiNotConfiguredError("DEEPSEEK_API_KEY is not set")

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("/ask test", user_id=603, language_code="en"))
    body = sent[0]["text"]
    assert "not configured" in body
    assert not any("\u0400" <= ch <= "\u04ff" for ch in body), body


# ----- /ai, /gpt, /chat, /kom_ai → DeepSeek (matches legacy) ----------
#
# Legacy ``bot.py`` mapped all four aliases to ONE DeepSeek assistant
# (``bot.py:38231`` — ``commands=['ai', 'ask', 'chat', 'kom_ai']`` →
# single ``ai_assistant``). The strangler-period OpenAI-billed branch
# is gone; the with-args form now reuses the ``/ask`` DeepSeek handler
# so the legacy parity is by-construction. Tests below pin the routing
# and the no-coin-charge posture (the only fake is ``AiService.ask``,
# never any economy seam).


@pytest.mark.parametrize("command", ["/ai", "/gpt", "/chat", "/kom_ai"])
async def test_with_prompt_routes_through_deepseek(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """All four aliases reach :meth:`AiService.ask` — the same DeepSeek
    code path ``/ask`` uses. No wallet seam is touched (this matches
    legacy: the AI commands were always free, only the daily quota
    gated them). If anyone re-introduces an OpenAI-billing branch on
    the ``/ai`` family, this test catches it via the upstream-call
    counter — exactly one DeepSeek call per invocation."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    captured_prompts: list[str] = []

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        captured_prompts.append(prompt)
        return "deepseek-ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update(f"{command} hello"))
    assert result is not UNHANDLED
    assert captured_prompts == ["hello"]
    assert "deepseek-ok" in sent[0]["text"]


async def test_ai_with_prompt_works_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy allowed group invocation for ``/ai`` / ``/gpt`` /
    ``/chat`` / ``/kom_ai`` (only ``/ask`` was private-only). The new
    pipeline preserves that — group calls do NOT fall through to the
    (now-dead) legacy."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "group ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("/gpt hi", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "group ok" in sent[0]["text"]


# Removed at the DeepSeek-realignment commit:
#
#   _fake_openai_response / _patch_openai_client / _seed_wallet / _balance
#   test_openai_chat_success_charges_and_renders[/ai|/gpt|/chat]
#   test_openai_chat_escapes_html_in_answer
#   test_openai_chat_missing_key_renders_not_configured
#   test_openai_chat_insufficient_balance_refuses_before_upstream
#   test_openai_chat_upstream_error_does_not_charge
#   test_openai_chat_works_in_group
#
# All asserted contracts of an OpenAI-billed ``/ai`` family that the
# strangler iteration carried but legacy never had. Re-introducing any
# of them is a regression against legacy, not a coverage gap.


@pytest.mark.parametrize("command", ["/ai", "/gpt", "/chat", "/kom_ai"])
async def test_ai_bare_in_group_renders_help(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    command: str,
) -> None:
    """Bare AI aliases in a group now render the help card (legacy
    answered every bare alias in any chat). With the fallback bridge
    gone, the bare forms must not silently no-op."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(command, chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "/ask" in sent[0]["text"]


# Sanity check: the construction of a real OpenAiConfig with a literal
# api_key SecretStr is what the handler relies on at the edge. Pinning
# the contract here means a future refactor that drops the SecretStr
# wrapping surfaces as a typed test failure, not a runtime AttributeError.
def test_openai_config_carries_secret() -> None:
    cfg = OpenAiConfig(OPENAI_API_KEY=SecretStr("sk-xyz"))
    assert cfg.api_key is not None
    assert cfg.api_key.get_secret_value() == "sk-xyz"


async def test_ask_truncates_long_answer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "x" * 10_000

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("/ask long"))
    body = sent[0]["text"]
    # 3997 + ellipsis = 3998 units. Telegram's 4096 cap is safe.
    #
    # Measured with ``parsed_length``, not ``len``: this answer is a run
    # of ASCII so the two agree here, but the pin must state the count
    # Telegram actually applies — see
    # ``test_an_emoji_dense_answer_is_cut_to_telegrams_own_count`` (#1972).
    assert parsed_length(body) <= TELEGRAM_TEXT_LIMIT
    assert body.endswith("…")


# One emoji per 39 Cyrillic characters — a model answer with a decorated
# heading every couple of lines, which is the ordinary shape of a long
# DeepSeek reply in Russian.
_EMOJI_BLOCK = ("😀" + "а" * 39) * 100


async def test_an_emoji_dense_answer_is_cut_to_telegrams_own_count(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1972: the ceiling is UTF-16 units, so ``len`` under-counts.

    An astral character is ONE ``len`` character and TWO units against
    Telegram's 4096 — the miscount ``utils.render.utf16_length`` was
    written to name. ``_truncate`` leaves 98 units of headroom, so 99
    emoji in the kept prefix are enough to spend it: this answer keeps
    100 and lands on 4098.

    Nothing downstream rescues it. ``api_length_guard`` logs the
    over-length body and deliberately does NOT truncate,
    ``api_parse_mode_fallback`` re-sends only on parse errors, and
    "message is too long" is not in ``errors._BENIGN_REJECT_MARKERS``
    — so the answer the user paid tokens for is replaced by the generic
    error card. On a cached prompt (``ai.py:466``) that repeats every
    time.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return _EMOJI_BLOCK

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("/ask long"))
    body = sent[0]["text"]

    assert body.endswith("…"), "the answer was not truncated at all"
    assert parsed_length(body) <= TELEGRAM_TEXT_LIMIT, (
        f"Telegram measures {parsed_length(body)} units, cap is {TELEGRAM_TEXT_LIMIT}"
    )
    # And the cut is still between characters: a lone surrogate is not
    # encodable and would fail on exactly the input this guards.
    body.encode("utf-16-le")


# ----- #298: the cut must not land inside the markup ------------------
#
# ``_truncate`` slices at a fixed offset. Whether that offset falls
# inside a tag depends entirely on whether the conversion ran first, and
# the existing test above cannot see the difference: a 10 000-character
# run of ``x`` converts to itself, so the slice is safe either way. The
# two below are built so the boundary lands *inside* generated markup.


async def test_ask_truncation_does_not_split_a_generated_tag(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``**bold**`` run straddling the limit must not leave a half tag.

    3990 plain characters, then the bold marker. Converted first, offset
    3997 lands seven characters into ``<b>жирный ...`` and the message
    goes out carrying an opening tag with no close — Telegram answers
    "can't parse entities", the parse-mode fallback middleware re-sends
    with formatting off, and the reader gets a wall of literal tags.
    Truncated first, the slice can at worst orphan the ``**`` marker,
    which ``markdown_to_html`` leaves as plain text by design.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "а" * 3990 + "**жирный хвост**" + "ещё" * 200

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("/ask long"))
    body = sent[0]["text"]

    assert body.count("<b>") == body.count("</b>")
    assert body.count("<code>") == body.count("</code>")
    # No dangling fragment of a tag at the very end either.
    assert not re.search(r"<[^>]*$", body)
    assert body.endswith("…")


async def test_ask_truncation_does_not_split_an_html_entity(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same cut, one layer down: ``&`` becomes five characters.

    Escaping expands ``&`` to ``&amp;``, so slicing converted text can
    stop at ``&a`` — which Telegram rejects for the same reason a half
    tag does. Cutting the raw answer means every ``&`` that survives is
    escaped whole.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "б" * 3995 + "&&&&&" + "хвост" * 100

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("/ask amp"))
    body = sent[0]["text"]

    # Every ``&`` present must open a complete entity.
    assert not re.search(r"&(?!(?:[a-zA-Z]+|#\d+);)", body)
    assert body.endswith("…")


# ----- M-P-2: ai_daily_requests daily quota ---------------------------


async def test_no_key_configured_does_not_burn_a_quota_slot(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A bot with no provider key must refuse WITHOUT spending quota (#419).

    The slot is consumed before the upstream call and then committed on
    purpose, so the session middleware's rollback cannot return it. But
    ``AiService.ask_with_context`` raises ``AiNotConfiguredError`` on its
    first line — no network, nothing to protect against. Charging for
    that meant a user could spend a whole day's allowance discovering
    that the feature is switched off, and stay locked out until midnight
    UTC on the very day the owner finally set the key. Production ran
    exactly this way: ``ai_daily_requests`` held real rows for dates on
    which no key existed.

    ``handlers.quotes`` already guarded this (quotes.py:206); ``/ai``
    did not. ``/ai_limits`` is the read-only witness — checking a quota
    must not consume it either.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase, UsersBase],
        ai_quota=AiQuotaSettings(free_daily_limit=3),
    )
    sent = capture_outgoing(bot)

    for _ in range(3):
        await dispatcher.feed_update(bot, _update("/ask q"))
    assert len(sent) == 3, sent
    assert all("не настроен" in m["text"] for m in sent), sent

    sent.clear()
    await dispatcher.feed_update(bot, _update("/ai_limits"))
    body = sent[0]["text"]
    assert "Использовано: 0 запросов" in body, body
    assert "Осталось: 3 из 3" in body, body


async def test_ask_over_quota_rejected_for_free_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call past the free daily ceiling is refused with the
    quota-exceeded copy.

    Legacy-parity policy (backlog L-69): the shipped default is
    free=10/day, but that equals the per-minute rate-limit bucket, so
    this test pins a *small* explicit quota (free=3) to exercise the
    quota gate independently of the rate limiter. The 4th call must
    surface the operator-side ceiling, NOT silently swallow the prompt
    and burn DeepSeek budget.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI,
        schemas=[EconomyBase, UsersBase],
        ai_quota=AiQuotaSettings(free_daily_limit=3),
    )
    sent = capture_outgoing(bot)

    upstream_calls = 0

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        nonlocal upstream_calls
        upstream_calls += 1
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    # 3 allowed calls
    for _ in range(3):
        await dispatcher.feed_update(bot, _update("/ask q"))

    # 4th must short-circuit before upstream
    sent.clear()
    await dispatcher.feed_update(bot, _update("/ask q"))
    body = sent[0]["text"]
    assert "исчерпан" in body or "exhausted" in body
    assert "4/3" in body
    # Upstream NOT called on the rejected attempt
    assert upstream_calls == 3


async def test_quota_stays_spent_when_the_model_call_blows_up(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot spent on a call that then failed must stay spent.

    ``AiQuotaService`` says so in as many words — the ceiling exists to
    cap operator cost, and we already paid the latency and possibly the
    metered request. But the consumption used to live in the update's
    transaction, so an exception anywhere downstream sent it back via
    the session middleware's rollback: a broken upstream turned the
    daily limit into no limit at all, and every retry cost money again.
    The handler now commits the slot (:class:`db.session.Checkpoint`)
    before it calls out — which is also what stops the write lock from
    being held for the whole model call.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI,
        schemas=[EconomyBase, UsersBase],
        ai_quota=AiQuotaSettings(free_daily_limit=3),
    )
    sent = capture_outgoing(bot)

    async def exploding_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        raise RuntimeError("deepseek is down")

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", exploding_ask)

    for _ in range(3):
        with suppress(RuntimeError):
            await dispatcher.feed_update(bot, _update("/ask q"))

    # The upstream comes back — but the day's three slots are gone.
    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)
    sent.clear()
    await dispatcher.feed_update(bot, _update("/ask q"))

    body = sent[0]["text"]
    assert "исчерпан" in body or "exhausted" in body
    assert "4/3" in body


# ----- #1597: a failed upstream call ---------------------------------
#
# The service used to answer a failed call with one of seven hardcoded
# Russian sentences, and the handler told a real answer from a degraded
# one by testing for a leading ``❌``. Both are gone: the failure now
# arrives as ``AiRequestError`` and the copy is chosen where ``lang``
# is in scope.


@pytest.mark.parametrize(("lang", "user_id"), [("ru", 15971), ("en", 15972)], ids=["ru", "en"])
async def test_a_failed_call_is_reported_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    user_id: int,
) -> None:
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def failing_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        raise ai_service_module.AiRequestError("timeout")

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", failing_ask)

    result = await dispatcher.feed_update(
        bot,
        _update("/ask q", user_id=user_id, language_code=None if lang == "ru" else "en"),
    )
    assert result is not UNHANDLED
    assert sent[0]["text"] == t("h_ai_error_timeout", lang)


async def test_a_rejected_credential_does_not_reach_the_reader(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401/402 are the owner's problem, and the reader is not told which.

    The old copy said «неверный API-ключ» / «исчерпан баланс» to whoever
    typed /ask — the provider account's state, handed to anyone in the
    group. Now both map to one neutral line and the status goes to the
    log instead.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    for status in (401, 402):

        async def failing_ask(self: Any, prompt: str, _status: int = status, **_kwargs: Any) -> str:
            raise ai_service_module.AiRequestError("http", status=_status)

        monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", failing_ask)
        sent.clear()
        await dispatcher.feed_update(bot, _update("/ask q", user_id=15973))

        body = sent[0]["text"]
        assert body == t("h_ai_error_unavailable", "ru")
        assert "ключ" not in body
        assert "баланс" not in body


async def test_a_failed_call_never_enters_the_conversation_memory(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the ``❌`` marker used to buy, the early return buys now.

    An error text recorded as an answer would be fed back to the model
    as context on the next turn and served from the response cache on a
    repeat of the same question — an outage would outlive itself.

    The reply is asserted too, and not as decoration: an empty memory
    is also what an UNCAUGHT exception leaves behind, because the
    errors router swallows it before the recording line. Pinning the
    localized text is what tells the two apart.
    """
    uid = 15974
    _MEMORY_STORE.clear(uid, uid)
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def failing_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        raise ai_service_module.AiRequestError("http", status=503)

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", failing_ask)

    await dispatcher.feed_update(bot, _update("/ask сколько времени", user_id=uid))

    assert sent[0]["text"] == t("h_ai_error_http", "ru")
    assert _MEMORY_STORE.size(uid, uid) == 0
    assert _RESPONSE_CACHE.get(uid, "сколько времени") is None


# ----- A-05: plain-text ``ком`` / ``ии`` trigger ----------------------
#
# Legacy ``ai_direct_handler`` (``bot.py:39639``) answered any plain
# message prefixed ``ии``/``ком`` in any chat. The strangler bridge
# deletion dropped that silently; these tests pin the restored routing.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ии привет", "привет"),
        ("ком, что нового?", "что нового?"),
        ("ком: считай", "считай"),
        ("ИИ Привет", "Привет"),  # case-insensitive prefix, original case kept
        ("ком", ""),  # bare → usage nudge
        ("ии", ""),
        ("ai", ""),
        ("привет всем", None),  # not addressed to the assistant
        ("/ai foo", None),  # slash command never matches
        ("", None),
        (None, None),
        ("комната", None),  # prefix must be a whole token
        ("kom", ""),
        ("kombat is fun", None),  # same whole-token rule as «комната»
    ],
)
def test_extract_ai_direct_question(text: str | None, expected: str | None) -> None:
    assert extract_ai_direct_question(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ai what is python", "what is python"),
        ("AI What Is Python", "What Is Python"),
        ("ai, what is python", "what is python"),
        ("ai: what is python", "what is python"),
        ("airplane mode", None),  # whole token, same rule as «комната»
    ],
)
def test_the_latin_marker_is_a_prefix_in_a_dm(text: str, expected: str | None) -> None:
    """I18N-3: "ai <question>" has to work the way «ии <вопрос>» does.

    Until #171 the latin marker was recognised only as a bare word, so
    an English user typing the obvious thing got no answer and no hint
    why — while the Russian spelling of the same sentence worked.
    """
    assert extract_ai_direct_question(text, is_group=False) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Addressed → answered, exactly like «ком, …».
        ("ai, what is python", "what is python"),
        ("ai: what is python", "what is python"),
        ("ai", ""),
        # Ordinary English chatter that merely opens with the word.
        ("AI is going to change everything", None),
        ("ai models keep getting cheaper", None),
    ],
)
def test_the_latin_marker_needs_addressing_in_a_group(text: str, expected: str | None) -> None:
    """The one asymmetry between «ии»/«ком» and "ai", and why it exists.

    «ии»/«ком» open a Russian sentence roughly never, so their space
    form is safe un-addressed. "AI" opens English sentences constantly —
    accepting it would have the bot answering a conversation nobody
    invited it into, which is the exact failure the whole plain-text
    layer is gated to avoid. A comma or a colon is the marker that the
    message is for the bot; in a DM none is needed because the whole
    message already is.
    """
    assert extract_ai_direct_question(text, is_group=True) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kom how to play duel", "how to play duel"),
        ("KOM How To Play Duel", "How To Play Duel"),
        ("kom, what is python", "what is python"),
        ("kom: what is python", "what is python"),
        ("kom", ""),
        ("kombat is fun", None),
        ("komodo dragons are real", None),
    ],
)
def test_the_latin_kom_works_like_the_cyrillic_one_in_a_group(
    text: str, expected: str | None
) -> None:
    """#206: the English FAQ's own example had to start working.

    "kom" gets «ком»'s rule rather than "ai"'s — space form accepted
    un-addressed, in a group as well as a DM — because it is not an
    English word and so cannot open an ordinary sentence by accident.
    The whole-token guard is what keeps "kombat"/"komodo" out.
    """
    assert extract_ai_direct_question(text, is_group=True) == expected


async def test_group_chatter_starting_with_ai_is_not_hijacked(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The end-to-end half of the rule above: through the real filter,
    a group message that merely begins with the word must reach no
    handler at all.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "AI is going to change everything",
            chat_type="supergroup",
            chat_id=-100,
            user_id=7,
        ),
    )
    assert result is UNHANDLED
    assert sent == []

    # …and the addressed form in the same wiring IS handled, which is
    # what makes the assertion above evidence rather than a test that
    # would pass with the whole AI router unregistered.
    addressed = await dispatcher.feed_update(
        bot,
        make_message_update(
            "ai, what is python",
            chat_type="supergroup",
            chat_id=-100,
            user_id=7,
        ),
    )
    assert addressed is not UNHANDLED


async def test_direct_trigger_routes_through_deepseek(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ``ии <question>`` (no slash) reaches DeepSeek with the
    prefix stripped — the legacy direct-trigger parity."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    captured: list[str] = []

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        captured.append(prompt)
        return "direct-ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("ии как дела?"))
    assert result is not UNHANDLED
    assert captured == ["как дела?"]
    assert "direct-ok" in sent[0]["text"]


async def test_direct_trigger_works_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy made Kom available to all group members via plain text."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "group-direct-ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("ком, привет", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert any("group-direct-ok" in m["text"] for m in sent)


async def test_direct_trigger_reads_a_photo_caption(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Addressing the bot in a photo's caption is the same trigger.

    Telegram puts the words of a message that carries a picture in
    ``caption`` and leaves ``text`` empty, so a filter reading only
    ``text`` never fired: «ком, что тут не так?» sent under a screenshot
    got no reply and no hint why — indistinguishable from the bot being
    down. The prefix must still be stripped, exactly as for typed text.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    captured: list[str] = []

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        captured.append(prompt)
        return "caption-ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(
        bot,
        _update("ком, что тут не так?", chat_type="supergroup", as_caption=True),
    )

    assert result is not UNHANDLED
    assert captured == ["что тут не так?"]
    assert any("caption-ok" in m["text"] for m in sent)


async def test_bare_direct_trigger_shows_hint_without_upstream(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``ком`` nudges for a question and must NOT burn the model."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    upstream_calls = 0

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        nonlocal upstream_calls
        upstream_calls += 1
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("ком"))
    assert result is not UNHANDLED
    assert "ком" in sent[0]["text"] and "ии" in sent[0]["text"]
    assert upstream_calls == 0


async def test_bare_direct_trigger_hint_follows_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``ком`` from an English-speaking caller.

    The English copy echoes back the *latin* marker, which #206 added
    for exactly this reader: before it, ``ии``/``ком`` were the only
    prefixes, so the card had to fall back to ``/ask`` — an
    ``ai <question>`` example would not have fired in a group, and the
    Cyrillic spellings are not typeable on an English keyboard.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    upstream_calls = 0

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        nonlocal upstream_calls
        upstream_calls += 1
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("ком", user_id=604, language_code="en"))
    body = sent[0]["text"]
    assert "Put the question in the same message" in body
    assert not any("\u0400" <= ch <= "\u04ff" for ch in body), body
    assert upstream_calls == 0


async def test_plain_chatter_is_not_stolen(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal message not addressed to the assistant stays UNHANDLED —
    the trigger must only fire on the ``ии``/``ком`` prefix."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("AI must not be called for plain chatter")

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(bot, _update("привет всем"))
    assert result is UNHANDLED
    assert sent == []


# --- #1345: the assembled system prompt follows the reader's language ---


async def test_english_system_prompt_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole briefing, not just the answer-language line, is English.

    The unit tests pin each piece separately; this pins the assembly,
    which is where #1345 actually bit — persona, guard, answer-language
    line, mode-hint block and time block are concatenated here, and a
    single Russian survivor is enough to pull a long answer back into
    Russian.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    capture_outgoing(bot)

    captured: list[str] = []

    async def fake_ask(self: Any, prompt: str, **kwargs: Any) -> str:
        captured.append(kwargs["system_prompt"])
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(
        bot, _update("/ask what time is it", user_id=650, language_code="en")
    )
    assert result is not UNHANDLED
    assert captured, "the model path was not reached"
    system_prompt = captured[0]
    assert "Reply in English only" in system_prompt
    assert not any("\u0400" <= ch <= "\u04ff" for ch in system_prompt), system_prompt


async def test_english_group_system_prompt_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same, through the group path, which adds the group block."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    capture_outgoing(bot)

    captured: list[str] = []

    async def fake_ask(self: Any, prompt: str, **kwargs: Any) -> str:
        captured.append(kwargs["system_prompt"])
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    result = await dispatcher.feed_update(
        bot,
        _update(
            "/ask roll a die",
            chat_type="supergroup",
            user_id=651,
            language_code="en",
        ),
    )
    assert result is not UNHANDLED
    assert captured, "the model path was not reached"
    system_prompt = captured[0]
    assert not any("\u0400" <= ch <= "\u04ff" for ch in system_prompt), system_prompt


async def test_russian_system_prompt_is_still_russian(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 must not have flipped the default the other way.

    Every new ``lang`` parameter defaults to ``ru``; this is the guard
    that a missed call site would show up as an English briefing for a
    Russian user rather than as a silent pass.
    """
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    capture_outgoing(bot)

    captured: list[str] = []

    async def fake_ask(self: Any, prompt: str, **kwargs: Any) -> str:
        captured.append(kwargs["system_prompt"])
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, _update("/ask сколько времени", user_id=652))
    assert captured, "the model path was not reached"
    system_prompt = captured[0]
    assert "Отвечай только на русском" in system_prompt
    assert "IMPORTANT: do not invent facts" not in system_prompt
