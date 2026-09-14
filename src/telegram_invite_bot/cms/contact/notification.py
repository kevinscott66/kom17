"""The Telegram message the operator receives for one web submission.

Separate from the page renderer because it answers a different question
— what the *operator* sees — and because it is the half with the wire
limit on it. A web form that accepts 2000 characters and a Telegram
message that accepts 4096 units are not the same ceiling: an emoji
costs one character in the form and two units on the wire, and escaping
turns one ``&`` into five. So the raw text is clamped in UTF-16 units
before it is escaped, and the assembled message is measured after
entity parsing.

Deliberately **not** included: the sender's IP address, user agent or
any other request metadata. It would be the only personal data the
service collects that the sender did not type, the privacy policy would
have to grow a clause for it, and it buys the operator nothing they can
act on. The throttle already handles the abuse case, and it holds that
data for minutes rather than forwarding it into a chat history forever.
"""

from __future__ import annotations

import html as html_lib
from typing import Final

from telegram_invite_bot.utils.render import (
    TELEGRAM_TEXT_LIMIT,
    clamp_utf16,
    parsed_length,
)

#: Always Russian: there is exactly one reader, and they are the bot's
#: owner. The sender's page language is reported as a field instead, so
#: the operator knows which language to answer in.
_HEADING: Final[str] = "📬 <b>Обращение с сайта</b>"
_REPLY_TO_LABEL: Final[str] = "Ответить"
_LANG_LABEL: Final[str] = "Язык страницы"

#: Room left for the parts whose length is not known until render time
#: (the labels, the newlines, the language tag). Generous on purpose —
#: the cost of over-reserving is a few trimmed characters at the end of
#: a 2000-character message; the cost of under-reserving is a 400 from
#: Telegram and an obligation the operator never learns about.
_OVERHEAD_MARGIN: Final[int] = 256

#: Marks a clamped body so the operator can tell "they wrote this much"
#: from "we cut it here".
_ELLIPSIS: Final[str] = "\n\n[…сообщение обрезано по лимиту Telegram]"


def build_admin_message(*, message: str, reply_to: str, lang: str) -> str:
    """The operator's notification, as HTML-parse-mode text.

    Both inputs are raw sender-controlled text. Everything that reaches
    the output goes through :func:`html.escape` — the message is sent
    with ``parse_mode="HTML"``, so an unescaped ``<b>`` in a submission
    would render as markup in the operator's chat, and an unescaped
    unbalanced ``<`` would make Telegram reject the whole delivery.
    """
    lang_tag = "EN" if lang == "en" else "RU"
    head = "\n".join(
        (
            _HEADING,
            "",
            f"<b>{_REPLY_TO_LABEL}:</b> {html_lib.escape(reply_to.strip())}",
            f"<b>{_LANG_LABEL}:</b> {lang_tag}",
            "",
        )
    )
    budget = TELEGRAM_TEXT_LIMIT - parsed_length(head) - _OVERHEAD_MARGIN
    body = message.strip()
    clamped = clamp_utf16(body, budget)
    tail = _ELLIPSIS if clamped != body else ""
    return head + html_lib.escape(clamped) + tail


def fits_telegram(text: str) -> bool:
    """Whether ``text`` is inside the ceiling Telegram actually applies.

    Exposed for the test that pins :func:`build_admin_message` against
    adversarial input (2000 astral-plane characters, 2000 ampersands);
    the sender is the one choosing the input, so "it fits in practice"
    is not a property worth trusting without an assertion.
    """
    return parsed_length(text) <= TELEGRAM_TEXT_LIMIT
