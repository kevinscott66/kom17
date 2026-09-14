"""Unit tests for the shared group-only refusal (#122).

:func:`~telegram_invite_bot.handlers.group_only.handle_group_only` is
the private-chat twin every group-pinned command family registers. It
does one thing, and the two properties worth pinning are both about
what goes into the reply:

* the alias echoed back is the one the caller actually typed, not a
  canonical name — someone who typed ``/пары`` must not be told about
  ``/marriages``;
* it is HTML-escaped. The value comes from ``CommandObject.command``,
  which aiogram has already matched against the registration's own
  allowlist, so today it can only be one of our words. That's an
  invariant of the ``Command`` filter, not of this function, and this
  function is what renders into an HTML message.

The handler only touches ``message.answer``, so a duck-typed stub is
enough.
"""

from __future__ import annotations

from typing import Any

import pytest

from telegram_invite_bot.handlers import group_only as group_only_mod
from telegram_invite_bot.i18n import t

handle_group_only: Any = group_only_mod.handle_group_only


class FakeMessage:
    """Records every ``answer`` call."""

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


def _command(word: str) -> Any:
    """Minimal ``CommandObject`` stand-in — only ``.command`` is read."""
    return type("Cmd", (), {"command": word})()


@pytest.mark.parametrize(
    "word",
    ["duel", "дуэль", "accept", "принять", "marriages", "браки", "пары", "activities"],
)
async def test_echoes_the_alias_typed(word: str) -> None:
    message = FakeMessage()
    await handle_group_only(message, _command(word), "ru")
    assert message.answers == [t("h_group_only_command", "ru", command=word)]
    assert f"/{word}" in message.answers[0]


async def test_answers_in_the_callers_language() -> None:
    ru, en = FakeMessage(), FakeMessage()
    await handle_group_only(ru, _command("duel"), "ru")
    await handle_group_only(en, _command("duel"), "en")
    assert ru.answers[0] != en.answers[0]
    assert "только в группе" in ru.answers[0]
    assert "only works in a group" in en.answers[0]


async def test_escapes_html_in_the_alias() -> None:
    """Defence in depth: the alias reaches an HTML-parsed message.

    The ``Command`` filter constrains the value today, but the alias
    lists are one regexp away from admitting something else, and the
    cost of escaping is nil.
    """
    message = FakeMessage()
    await handle_group_only(message, _command("<b>x</b>"), "ru")
    assert "&lt;b&gt;x&lt;/b&gt;" in message.answers[0]
    assert "<b>x</b>" not in message.answers[0]
