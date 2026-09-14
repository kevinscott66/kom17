"""End-to-end ``/faq2`` (Stage 22).

What's worth proving:

* Both aliases (``/faq2`` and ``/faq_2``) route to the new handler
  and the user sees the part-2 body. ``/faq`` (part 1) must still
  fall through — that's the regression pin against accidentally
  catching too much under the same router.
* Language picks off ``message.from_user.language_code`` — ``en``
  client gets the English body, missing/unknown header defaults to
  RU (legacy ``get_user_language`` semantics). The default-RU branch
  is the one users hit when their TG client predates the language-
  code header.
* Body is HTML, not Markdown — a literal ``**bold**`` slipping
  through would mean the Markdown→HTML conversion regressed on the
  YAML side, which would print asterisks to every FAQ reader.
* Works in groups (heartbeat parity — legacy gates on role, not
  chat type; the new pipeline doesn't gate at all yet).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.config.settings import HelpConfig
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


async def test_faq2_renders_part2_body(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Happy path — Russian default, body contains the section
    headers users expect to see plus the footer call-to-action.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update("/faq2"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    # Section headers — part 2 covers games/VIP/groups/privacy/support/
    # ads/AI. ``СТАТИСТИКА`` used to be here and is deliberately gone:
    # T-041 traded the stats walkthrough (a command list in prose, now
    # on the site) for privacy and fairness, which the site can't cover.
    assert "ИГРЫ" in body
    assert "ПРИВАТНОСТЬ" in body
    assert "ПОДДЕРЖКА" in body
    # Footer — pinned because it's a separate YAML legacy key that we
    # concatenated into h_faq_part2; if a future edit forgets the
    # join, this assertion catches the regression.
    assert "/support" in body
    assert "/feedback" in body


async def test_faq_part2_fits_in_one_telegram_message(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Telegram rejects a body over 4096 characters.

    Part 2 is the longest static card in the bot and sits within a few
    hundred characters of the ceiling in both languages, so a single
    added paragraph can push it over. Unpinned, that surfaces as a
    silent send failure in production — the user taps "continue" and
    nothing happens. Pinned here, it surfaces as a red test.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/faq2"))
    # Distinct sender: LanguageMiddleware memoises the resolved language
    # per user id, so reusing user 1 would measure the RU card twice.
    await dispatcher.feed_update(
        bot,
        make_message_update("/faq2", user_id=4244, language_code="en", update_id=2, message_id=2),
    )
    assert len(sent) == 2
    for message in sent:
        visible = re.sub(r"<[^>]+>", "", message["text"])
        assert len(visible) <= 4096, f"FAQ part 2 is {len(visible)} chars"


async def test_faq2_body_is_html_not_markdown(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Legacy stored part 2 as Markdown ``**bold**``; we converted to
    HTML ``<b>...</b>`` because the bot runs ``parse_mode=HTML``.
    Markdown asterisks leaking through would mean the conversion was
    reverted and users see literal ``**``.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/faq2"))
    body = sent[0]["text"]
    assert "<b>" in body
    assert "</b>" in body
    assert "**" not in body


async def test_faq2_underscore_alias_routes(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/faq_2`` is the legacy underscore alias (bot.py:35451 lists
    both ``faq2`` and ``faq_2``). Pinned separately from the bare
    ``/faq2`` so a Command-filter typo can't silently drop it.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update("/faq_2"))
    assert result is not UNHANDLED
    assert "ИГРЫ" in sent[0]["text"]


async def test_faq2_english_when_language_code_en(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``language_code=en`` on the sender → English body. Verifies
    the lang-resolution branch that doesn't touch the DB.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/faq2", language_code="en"))
    body = sent[0]["text"]
    assert "GAMES" in body
    assert "PRIVACY" in body
    # Should NOT contain Russian-only section headers.
    assert "ПОДДЕРЖКА" not in body


async def test_faq2_defaults_to_russian_without_language_code(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No ``language_code`` header (old client) → RU body. Matches
    legacy ``get_user_language`` returning ``ru`` on a missing row.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/faq2"))
    assert "ИГРЫ" in sent[0]["text"]


async def test_faq2_works_in_group_chat(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Heartbeat parity — chat-type-agnostic, same as /ping. Pinned
    because the natural reflex (read-only personal help → private-
    only) would be wrong here; legacy answers in groups too.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/faq2", chat_type="supergroup", chat_id=-100),
    )
    assert result is not UNHANDLED
    assert "ИГРЫ" in sent[0]["text"]


async def test_faq2_does_not_swallow_bare_faq_in_groups(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """REGRESSION PIN.

    Stage 22 owns ``/faq2`` only. The bare ``/faq`` is owned by
    ``handlers/support`` in BOTH private and group chats (group /faq
    matched legacy — bot.py:35415 "Один ответ в группе" — and was
    restored once the legacy bridge was removed). The faq2 router
    here MUST NOT accidentally catch ``/faq`` — a Command-filter that
    string-prefix-matched ``faq`` would silently capture both
    ``/faq`` and ``/faq2`` and the part-1 → part-2 button flow
    would die.

    Proof: group ``/faq`` must render the FAQ *part-1* card
    ("ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ" title), NOT the *part-2* body
    ("ИГРЫ"). If faq2 had swallowed it we'd see part 2 instead.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/faq", chat_type="supergroup", chat_id=-100),
    )
    assert result is not UNHANDLED
    assert "ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ" in sent[0]["text"]
    # faq2's part-2 body leads with "ИГРЫ" — its absence proves the
    # faq2 router did not capture the bare /faq. Part 1's table of
    # contents names the section in title case ("Игры"), never the
    # uppercase header, so this stays a clean discriminator.
    assert "ИГРЫ" not in sent[0]["text"]


# ── RR-6 #70: the "all commands" guide button under part 2 ──────────


async def test_faq2_has_no_guide_button_without_url(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Unconfigured deployment → text only.

    Telegram rejects a URL button with an empty URL, so "no URL
    configured" must mean "no button", not "button pointing
    nowhere". This is also what legacy renders when
    ``get_telegraph_commands_url`` returns ``None``.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/faq2"))
    assert sent[0]["markup"] is None


async def test_faq2_guide_button_uses_language_specific_url(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Configured deployment → one URL button, per-language target.

    Legacy hard-codes the Russian label for English users
    (bot.py:35442); the port localises both the label and the URL, so
    both halves are pinned here. The RU/EN URLs are deliberately
    different so a copy-paste bug that always reads the RU field
    fails instead of passing by coincidence.
    """
    help_config = HelpConfig(
        TELEGRAPH_COMMANDS_URL="https://example.test/ru/commands",
        TELEGRAPH_COMMANDS_URL_EN="https://example.test/en/commands",
    )
    bot, dispatcher, _ = await make_wired(help_config=help_config)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/faq2"))
    # Distinct sender: LanguageMiddleware memoises the resolved
    # language per user id for 300 s, so reusing the default user
    # would silently serve the RU answer to the EN request and the
    # per-language assertion below would pass for the wrong reason.
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/faq2",
            user_id=2,
            language_code="en",
            update_id=2,
            message_id=2,
        ),
    )

    ru_button = sent[0]["markup"].inline_keyboard[0][0]
    assert ru_button.url == "https://example.test/ru/commands"
    assert ru_button.callback_data is None, "guide button must be a URL button"

    en_button = sent[1]["markup"].inline_keyboard[0][0]
    assert en_button.url == "https://example.test/en/commands"
    assert not re.search(r"[А-Яа-яЁё]", en_button.text)
