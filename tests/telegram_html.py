"""Validator for the Bot API's "HTML style" subset.

Telegram parses a message whole or not at all. One tag it does not
recognise — ``Usage: /modcfg <param> <value>`` — and ``sendMessage``
comes back ``400 Bad Request: can't parse entities: Unsupported start
tag "param"``. Not a mangled bubble: no bubble. The command answers
nothing and the traceback lands in the log, not in front of the user.

Two surfaces feed Telegram HTML and both are guarded against this,
which is why the check lives here rather than inside either one:

* ``tests/regression/test_i18n_html_safety.py`` — the i18n catalogue,
  rendered through :func:`telegram_invite_bot.i18n.t`;
* ``tests/regression/test_admin_card_html_safety.py`` — the
  ``/admin_*`` diagnostic cards, whose markup is built in Python and
  therefore never passes through the i18n escaping at all.

Two failure modes are reported, and only those two, because only those
two are fatal:

* a ``<…>`` Telegram does not know — it stops parsing and 400s;
* a known tag left open or closed out of order — same 400.

A bare ``&`` is NOT reported. Telegram accepts it (``👥 Roles &
permissions`` ships today), a good deal of that copy is button text
that is never parsed at all, and escaping it wholesale would be ~30
edits to satisfy a rule Telegram does not enforce.
"""

from __future__ import annotations

import re
from typing import Final

#: Bot API "HTML style". Anything else is an "Unsupported start tag".
ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "span",
        "tg-spoiler",
        "a",
        "tg-emoji",
        "code",
        "pre",
        "blockquote",
    }
)

_TAG: Final[re.Pattern[str]] = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*)?)>")


def telegram_html_errors(text: str) -> list[str]:
    """Everything Telegram would refuse to parse in ``text``."""
    errors: list[str] = []
    stack: list[str] = []
    pos = 0
    for match in _TAG.finditer(text):
        gap = text[pos : match.start()]
        if "<" in gap:
            errors.append(f"unescaped '<' in {gap!r}")
        pos = match.end()
        closing, name = match.group(1), match.group(2).lower()
        if name not in ALLOWED_TAGS:
            errors.append(f"unsupported tag <{name}>")
        elif not closing:
            stack.append(name)
        elif not stack:
            errors.append(f"</{name}> with nothing open")
        elif stack[-1] != name:
            errors.append(f"</{name}> closes <{stack.pop()}>")
        else:
            stack.pop()
    if "<" in text[pos:]:
        errors.append(f"unescaped '<' in {text[pos:]!r}")
    if stack:
        errors.append(f"never closed: {stack}")
    return errors
