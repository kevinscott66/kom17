"""FAQ rendering + ``/faq2`` (Stage 22, completed by RR-6 #70).

The FAQ is a **two-page card**. Legacy paged it because the whole thing
does not fit in one Telegram message (4096 chars): ``cmd_faq``
(bot.py:35415) sends title + table of contents + part 1 with an inline
"continue" button, and ``_send_faq_part2`` (bot.py:35434) sends the rest
with a URL button to the full command guide on the bot's site.

The port kept only the second page. ``/faq`` shipped a six-bullet RU-only
blurb and no guide link at all, so the command that new users reach for
first was the least finished surface in the bot. RR-6 #70 restores both
pages and the link.

This module owns the *rendering* for both pages so the three entry points
cannot drift:

* ``/faq``            → :func:`render_part1` (``handlers/support.py``)
* ``FaqContinue`` tap → :func:`render_part2` (``handlers/support.py``)
* ``/faq2``           → :func:`render_part2` (here)

Deliberate differences from legacy:

* **Both pages are bilingual.** Legacy hard-codes the Russian label
  ``"📄 Все команды (сайт)"`` on the guide button for English users too
  (bot.py:35442); here it is a normal i18n key.
* **Content is answered against the current command surface**, not
  copied verbatim — see the ``h_faq_part1`` comment in ``ru.yaml``.
* **Auto-delete** (legacy ``schedule_deletion(..., 120)``) is not ported:
  the scheduler doesn't exist in the new pipeline. A slightly louder chat,
  functionally identical. Tracked as deferred, not forgotten.
* **Access gate**: legacy calls ``ensure_user_access`` (a role check, not
  a chat-type check), which the new pipeline doesn't model. ``/cmdcfg``
  (``CommandAccessMiddleware``) is the per-group off-switch, and both
  ``faq`` and ``faq2`` already carry catalog entries there
  (``core/ranks.py`` id 3).

Language comes from the root ``LanguageMiddleware`` as ``data["lang"]``
(stored preference > Telegram ``language_code`` > ``"ru"``). No DB
session is opened here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import FaqContinue

log = logger.bind(component="handlers.faq")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import HelpConfig


def render_part1(lang: str) -> str:
    """Title + table of contents + the first four sections.

    The blank line between the TOC and the body comes from the leading
    newline baked into ``h_faq_part1`` (verbatim from legacy
    translations.py:1084, which starts with ``\\n🤔``) — keeping it means
    the old and new bots render identically side by side, which is the
    first thing an operator compares during a rollback.
    """
    return t("h_faq_title", lang) + "\n\n" + t("h_faq_toc", lang) + t("h_faq_part1", lang)


def render_part2(lang: str) -> str:
    """The remaining four sections + the footer.

    ``h_faq_part2`` bakes in the legacy ``part2 + "\\n\\n" + footer``
    join, so a caller cannot forget the blank line between them.
    """
    return t("h_faq_part2", lang)


def build_continue_markup(lang: str, help_config: HelpConfig | None = None) -> InlineKeyboardMarkup:
    """The continue-to-part-2 button, plus the command-list link.

    The link is on **both** pages deliberately. The FAQ answers what a
    command list cannot (how coins are earned, what withdrawal limits
    exist, what the bot stores); "which command does X" is the site's
    job, and a reader who wants that should not have to page through an
    answer they didn't ask for to find the door.

    The continue button carries no payload — see
    ``keyboards/builders/faq.py`` for why the callback deliberately has
    no ``user_id`` field.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=t("h_faq_continue_btn", lang),
                callback_data=FaqContinue().pack(),
            )
        ]
    ]
    url = help_config.guide_url(lang) if help_config is not None else None
    if url:
        rows.append([InlineKeyboardButton(text=t("h_faq_all_commands_btn", lang), url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_part2_markup(lang: str, help_config: HelpConfig | None) -> InlineKeyboardMarkup | None:
    """URL button to the full command guide, or ``None`` when unset.

    Resolution lives on :meth:`HelpConfig.guide_url` so ``/help`` and
    ``/faq`` cannot drift. The guide URL is optional config, and a URL
    button with no URL is not a thing Telegram accepts — so an
    unconfigured deployment simply gets the text without a button,
    exactly what legacy shows when ``get_telegraph_commands_url``
    returns ``None``.
    """
    if help_config is None:
        return None
    url = help_config.guide_url(lang)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("h_faq_all_commands_btn", lang), url=url)]]
    )


async def handle_faq2(message: Message, lang: str, help_config: HelpConfig | None = None) -> None:
    """Render part 2 directly — the ``/faq2`` shortcut past page one."""
    await message.answer(render_part2(lang), reply_markup=build_part2_markup(lang, help_config))
    log.bind(
        chat_id=message.chat.id if message.chat else None,
        lang=lang,
    ).info("/faq2 rendered")


def build_router(help_config: HelpConfig | None = None) -> Router:
    """No registry, no middleware — static text plus one optional URL.

    Two aliases — ``/faq2`` and ``/faq_2`` — exactly matching legacy
    ``commands=['faq2', 'faq_2']`` at bot.py:35451. ``ignore_case``
    keeps ``/FAQ2`` working (legacy registration is case-sensitive in
    pyTelegramBotAPI but Telegram clients lowercase commands on
    send, so the effective surface is the same).

    ``help_config`` defaults to ``None`` so older call sites keep
    working; they just render without the guide button.
    """

    async def _handle_faq2(message: Message, lang: str) -> None:
        await handle_faq2(message, lang, help_config)

    router = Router(name="faq")
    router.message.register(
        _handle_faq2,
        Command("faq2", "faq_2", ignore_case=True),
    )
    return router
