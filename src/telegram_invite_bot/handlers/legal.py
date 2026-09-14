"""``/legal`` — the documents card, with buttons at the public pages.

An acquiring bank's onboarding asks for three things to be reachable by
an ordinary user, not just by a reviewer with a direct link: the privacy
policy, the public offer, and a way to contact support. The pages
themselves live in :mod:`telegram_invite_bot.cms.legal` and are served
by the bot's own web process; this handler is the door to them from
inside Telegram, which is where our users actually are.

Everything degrades. With no ``WEBHOOK_URL`` there are no page buttons —
the card still renders and still names ``/support``, the in-bot ticket
system, which is by itself one of the three contact forms the bank
accepts. With no ``SUPPORT_USERNAME`` there is no direct-chat button.
What must never happen is the opposite failure: a URL button carrying a
scheme Telegram rejects takes down the whole ``sendMessage``, so every
URL is scheme-checked here before it reaches a button — the same
reasoning as :meth:`HelpConfig.guide_url`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from telegram_invite_bot.cms.paths import doc_path
from telegram_invite_bot.i18n import t

log = logger.bind(component="handlers.legal")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings

#: Telegram accepts ``http``/``https`` (and ``tg://``) on URL buttons and
#: rejects the message outright otherwise.
_WEB_SCHEMES = ("https://", "http://")


def doc_url(base: str | None, slug: str, lang: str) -> str | None:
    """Absolute URL of one legal page, or ``None`` when unusable.

    ``base`` is ``WEBHOOK_URL`` — an origin, not a full webhook path
    (:mod:`webhook.lifespan` appends the path itself). A deployment on
    long polling has none, and gets no button.
    """
    origin = (base or "").strip().rstrip("/")
    if not origin.startswith(_WEB_SCHEMES):
        return None
    return origin + doc_path(slug, lang)


def build_markup(settings: Settings, lang: str) -> InlineKeyboardMarkup | None:
    """Offer, policy, support page, support chat — whichever resolve.

    One button per row: these are long labels, and a row of two truncates
    to "Публичная о…" / "Политика ко…" on a phone, which is precisely the
    text a reader is looking for when they open this card.
    """
    base = settings.webhook.url
    rows: list[list[InlineKeyboardButton]] = []
    for slug, key in (("terms", "h_legal_btn_terms"), ("privacy", "h_legal_btn_privacy")):
        url = doc_url(base, slug, lang)
        if url:
            rows.append([InlineKeyboardButton(text=t(key, lang), url=url)])

    support_page = doc_url(base, "support", lang)
    if support_page:
        rows.append(
            [InlineKeyboardButton(text=t("h_legal_btn_support_page", lang), url=support_page)]
        )

    support_chat = settings.legal.support_url
    if support_chat:
        rows.append(
            [InlineKeyboardButton(text=t("h_legal_btn_support_chat", lang), url=support_chat)]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def render_card(settings: Settings, lang: str) -> str:
    """The card body. Never empty, never depends on config being set."""
    lines = [t("h_legal_title", lang), "", t("h_legal_body", lang), "", t("h_legal_ticket", lang)]
    if settings.legal.support_email:
        lines.append(t("h_legal_email", lang, email=settings.legal.support_email))
    if doc_url(settings.webhook.url, "terms", lang) is None:
        # No site: say so plainly rather than pointing at buttons that
        # aren't there. The ticket line above is still a live route.
        lines.extend(("", t("h_legal_no_site", lang)))
    return "\n".join(lines)


def build_router(settings: Settings) -> Router:
    """Static text plus optional URL buttons — no registry, no DB.

    Aliases cover what a user in a hurry actually types: ``/terms`` and
    ``/privacy`` are the two documents by name, ``/offer`` the Russian
    habit of calling the agreement "оферта", ``/docs`` the generic one.
    They all render the same card because the answer to "where is the
    privacy policy" and "where is the offer" is the same two taps, and
    splitting them into three cards would only hide each from the other.
    """

    async def _handle_legal(message: Message, lang: str) -> None:
        await message.answer(
            render_card(settings, lang),
            reply_markup=build_markup(settings, lang),
        )
        log.bind(chat_id=message.chat.id if message.chat else None, lang=lang).info(
            "/legal rendered"
        )

    router = Router(name="legal")
    router.message.register(
        _handle_legal,
        Command("legal", "terms", "privacy", "offer", "docs", ignore_case=True),
    )
    return router
