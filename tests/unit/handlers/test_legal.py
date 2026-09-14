"""``/legal`` — the in-bot door to the offer, the policy and support.

The card's job is to hold up under a *missing* configuration, not a
complete one: a deployment with no site origin and no support handle
must still render, still route the user somewhere real, and above all
must not build a URL button Telegram will reject — that failure mode
takes the whole message down, so the user loses the card instead of
losing a button.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.config.settings import LegalConfig
from telegram_invite_bot.handlers.legal import (
    build_markup,
    build_router,
    doc_url,
    render_card,
)
from telegram_invite_bot.i18n import t

LANGS = ("ru", "en")


class _Webhook:
    def __init__(self, url: str | None) -> None:
        self.url = url


class _Settings:
    """Only the two attributes the handler reads."""

    def __init__(self, url: str | None = "https://tgbot.delabs.space", **legal: object) -> None:
        self.webhook = _Webhook(url)
        self.legal = LegalConfig(**legal)  # type: ignore[arg-type]


# --- URL resolution -------------------------------------------------


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://tgbot.delabs.space", "https://tgbot.delabs.space/terms"),
        ("https://tgbot.delabs.space/", "https://tgbot.delabs.space/terms"),
        ("http://localhost:8080", "http://localhost:8080/terms"),
        (None, None),
        ("", None),
        ("   ", None),
        ("tgbot.delabs.space", None),
        ("javascript:alert(1)", None),
    ],
)
def test_doc_url_accepts_only_web_origins(base: str | None, expected: str | None) -> None:
    assert doc_url(base, "terms", "ru") == expected


def test_doc_url_carries_the_language_suffix() -> None:
    assert doc_url("https://x.y", "privacy", "en") == "https://x.y/privacy/en"
    assert doc_url("https://x.y", "privacy", "ru") == "https://x.y/privacy"


# --- keyboard -------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_full_config_renders_all_four_buttons(lang: str) -> None:
    markup = build_markup(_Settings(SUPPORT_USERNAME="@kom17_support"), lang)  # type: ignore[arg-type]
    assert markup is not None
    urls = [row[0].url for row in markup.inline_keyboard]
    assert urls == [
        f"https://tgbot.delabs.space{'/terms/en' if lang == 'en' else '/terms'}",
        f"https://tgbot.delabs.space{'/privacy/en' if lang == 'en' else '/privacy'}",
        f"https://tgbot.delabs.space{'/support/en' if lang == 'en' else '/support'}",
        "https://t.me/kom17_support",
    ]
    assert all(len(row) == 1 for row in markup.inline_keyboard), "one button per row — long labels"


def test_no_origin_leaves_only_the_support_chat_button() -> None:
    markup = build_markup(_Settings(url=None, SUPPORT_USERNAME="kom17_support"), "ru")  # type: ignore[arg-type]
    assert markup is not None
    assert [row[0].url for row in markup.inline_keyboard] == ["https://t.me/kom17_support"]


def test_nothing_configured_yields_no_keyboard_rather_than_an_empty_one() -> None:
    """An ``InlineKeyboardMarkup`` with zero rows is not a thing Telegram
    accepts — it must be ``None`` so the card sends as plain text."""
    assert build_markup(_Settings(url=None), "ru") is None  # type: ignore[arg-type]


def test_a_malformed_support_handle_never_reaches_a_button() -> None:
    """A pasted URL in ``SUPPORT_USERNAME`` would build ``t.me/https://…``."""
    markup = build_markup(_Settings(url=None, SUPPORT_USERNAME="https://t.me/x"), "ru")  # type: ignore[arg-type]
    assert markup is None


# --- card body ------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_card_always_names_the_ticket_command(lang: str) -> None:
    for settings in (_Settings(), _Settings(url=None), _Settings(SUPPORT_EMAIL="a@b.ru")):
        assert "/support" in render_card(settings, lang)  # type: ignore[arg-type]


@pytest.mark.parametrize("lang", LANGS)
def test_card_admits_it_when_the_pages_are_unreachable(lang: str) -> None:
    """Pointing at "the links below" with no links below is the one thing
    the card must not do."""
    assert t("h_legal_no_site", lang) in render_card(_Settings(url=None), lang)  # type: ignore[arg-type]
    assert t("h_legal_no_site", lang) not in render_card(_Settings(), lang)  # type: ignore[arg-type]


@pytest.mark.parametrize("lang", LANGS)
def test_email_line_appears_only_when_configured(lang: str) -> None:
    assert "a@b.ru" in render_card(_Settings(SUPPORT_EMAIL="a@b.ru"), lang)  # type: ignore[arg-type]
    # No mailbox configured → the whole line is absent, not an empty
    # "Почта поддержки:" with nothing after the colon.
    label = t("h_legal_email", lang, email="SENTINEL").replace("SENTINEL", "")
    assert label not in render_card(_Settings(), lang)  # type: ignore[arg-type]


@pytest.mark.parametrize("lang", LANGS)
def test_a_malformed_email_is_dropped_rather_than_printed(lang: str) -> None:
    assert "not an email" not in render_card(_Settings(SUPPORT_EMAIL="not an email"), lang)  # type: ignore[arg-type]


# --- wiring ---------------------------------------------------------


def test_every_documented_alias_is_registered() -> None:
    from aiogram.filters import Command

    router = build_router(_Settings())  # type: ignore[arg-type]
    names: set[str] = set()
    for handler in router.message.handlers:
        for flt in handler.filters or ():
            if isinstance(flt.callback, Command):
                names.update(c for c in flt.callback.commands if isinstance(c, str))
    assert names == {"legal", "terms", "privacy", "offer", "docs"}
