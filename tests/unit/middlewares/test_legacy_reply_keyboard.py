"""Retirement of the monolith's stuck reply keyboard.

Legacy built a persistent ``ReplyKeyboardMarkup`` for the owner and for
group admins (``bot.py:16426`` / ``bot.py:16434``, both
``one_time_keyboard=False``). The new pipeline sends no reply keyboards
at all, so nothing there could ever take it away, and its labels matched
no handler — not here, and not in legacy either.

:class:`LegacyReplyKeyboardMiddleware` closes both halves: it answers
with :class:`ReplyKeyboardRemove` (the only thing that actually clears
the keyboard from the client) and rewrites the tap to the ``/command``
that owns the surface now, the way ``TextAliasMiddleware`` does for the
legacy plain-text shortcuts.

Pinned here: the three labels map to live commands, the rewrite happens
only in a private chat and only outside an FSM step, and the middleware
neither consumes the update nor raises when the removal cannot be
delivered.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from aiogram.enums import ChatType
from aiogram.types import Message, ReplyKeyboardRemove

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.legacy_reply_keyboard import (
    LEGACY_BUTTONS,
    LegacyReplyKeyboardMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_CHAT = 4242


class _Msg(Message):
    """A message whose ``answer`` records instead of calling Telegram."""

    async def answer(self, text: str, **kwargs: Any) -> Any:  # type: ignore[override]
        sent: list[tuple[str, Any]] = self.__pydantic_extra__["sent"]  # type: ignore[index]
        if self.__pydantic_extra__["answer_fails"]:  # type: ignore[index]
            raise RuntimeError("bot blocked by the user")
        sent.append((text, kwargs.get("reply_markup")))
        return None


def _msg(
    text: str | None,
    chat_type: ChatType = ChatType.PRIVATE,
    *,
    is_bot: bool = False,
    answer_fails: bool = False,
) -> _Msg:
    return _Msg.model_construct(
        message_id=777,
        date=cast("Any", None),
        chat=cast("Any", SimpleNamespace(id=_CHAT, type=chat_type)),
        from_user=cast("Any", SimpleNamespace(id=42, is_bot=is_bot)),
        text=text,
        sent=[],
        answer_fails=answer_fails,
    )


def _sent(message: _Msg) -> Sequence[tuple[str, Any]]:
    return cast("Sequence[tuple[str, Any]]", message.__pydantic_extra__["sent"])  # type: ignore[index]


async def _run(event: _Msg, data: dict[str, Any] | None = None) -> Message:
    """Run the middleware and return the message the handler received."""
    seen: dict[str, Any] = {}

    async def _handler(ev: Any, d: dict[str, Any]) -> str:
        seen["event"] = ev
        return "ok"

    result = await LegacyReplyKeyboardMiddleware()(_handler, event, data or {})
    assert result == "ok"  # never consumes the update
    return cast("Message", seen["event"])


async def test_each_label_is_rewritten_to_its_command() -> None:
    for label, target in (
        ("👑 Панель разработчика", "/admin_panel"),
        # ``/groupadmin`` is group-only, and this keyboard only ever
        # existed in the private chat — ``/mygroups`` is the private
        # multi-group admin surface.
        ("🛡️ Панель администратора", "/mygroups"),
        ("🤖 Ком", "/ai"),
    ):
        out = await _run(_msg(label))
        assert out.text == target, label
        # The stale entities of the plain-text tap must not survive the
        # rewrite — the offsets no longer point anywhere sane.
        assert out.entities is None


async def test_tap_removes_the_keyboard() -> None:
    msg = _msg(
        "🤖 Ком",
    )
    await _run(msg, {"lang": "ru"})
    assert len(_sent(msg)) == 1
    text, markup = _sent(msg)[0]
    assert text == t("h_legacy_keyboard_retired", "ru")
    assert isinstance(markup, ReplyKeyboardRemove)


async def test_the_note_is_localised() -> None:
    msg = _msg("🤖 Ком")
    await _run(msg, {"lang": "en"})
    assert _sent(msg)[0][0] == t("h_legacy_keyboard_retired", "en")


async def test_label_match_ignores_case() -> None:
    out = await _run(_msg("👑 ПАНЕЛЬ РАЗРАБОТЧИКА"))
    assert out.text == "/admin_panel"


async def test_surrounding_whitespace_is_tolerated() -> None:
    out = await _run(_msg("  🤖 Ком  "))
    assert out.text == "/ai"


async def test_group_chat_is_untouched() -> None:
    # These keyboards were never shown in a group; matching there would
    # let any member open the panel surface by typing the label.
    msg = _msg("👑 Панель разработчика", chat_type=ChatType.SUPERGROUP)
    assert await _run(msg) is msg
    assert not _sent(msg)


async def test_fsm_step_keeps_its_input() -> None:
    # Same rule as TextAliasMiddleware: a running text step owns whatever
    # the user sends, keyboard or not.
    msg = _msg("🤖 Ком")
    assert await _run(msg, {"raw_state": "Withdraw:amount"}) is msg
    assert not _sent(msg)


async def test_idle_fsm_still_rewrites() -> None:
    out = await _run(_msg("🤖 Ком"), {"raw_state": None})
    assert out.text == "/ai"


async def test_ordinary_text_is_untouched() -> None:
    msg = _msg("панель")
    assert await _run(msg) is msg
    assert not _sent(msg)


async def test_non_text_message_is_untouched() -> None:
    msg = _msg(None)
    assert await _run(msg) is msg


async def test_bot_sender_is_untouched() -> None:
    msg = _msg("🤖 Ком", is_bot=True)
    assert await _run(msg) is msg


async def test_undeliverable_removal_still_routes_the_tap() -> None:
    # A blocked/deleted chat must not swallow the tap: the keyboard stays
    # for another try, but the command the user asked for still runs.
    out = await _run(_msg("🤖 Ком", answer_fails=True))
    assert out.text == "/ai"


async def test_every_target_is_a_slash_command() -> None:
    assert all(target.startswith("/") for target in LEGACY_BUTTONS.values())
    assert all(label == label.casefold() for label in LEGACY_BUTTONS)
