"""Unit tests for the chat-type refusal wrapper (#123).

``handlers/chat_scope.py`` has two halves and they fail differently.

:func:`handle_private_only` is the reply itself — the mirror of
``group_only``'s handler, plus a deep-link button, so the tests below
mirror ``test_group_only`` and add the button.

:func:`with_chat_type_refusal` is structural, and its failure modes are
the interesting ones: harvesting the wrong words means the bot refuses
a command that works, harvesting none means the silence stays, and a
filter on the wrapper itself would gate the worker it is supposed to
leave alone. Those are checked by building throwaway routers rather
than the production tree — ``tests/regression/test_chat_scope_coverage``
is what checks the real one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from magic_filter import MagicFilter

from telegram_invite_bot.handlers import chat_scope
from telegram_invite_bot.handlers.chat_scope import (
    handle_private_only,
    with_chat_type_refusal,
)
from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.i18n import t

#: Called with duck-typed stubs, as in ``test_group_only``: the handler
#: reads ``message.answer``, ``message.chat.id`` and ``bot.me`` and
#: nothing else, and a real ``Message``/``Bot`` would cost a validated
#: payload and an HTTP session to say the same thing.
_refuse: Any = handle_private_only

#: The group the refusal is spoken in — the id the button has to carry.
_GROUP_ID = -1001234567890


class FakeMessage:
    """Records ``answer`` calls with their markup."""

    def __init__(self, chat_id: int = _GROUP_ID) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs.get("reply_markup")))


def _command(word: str) -> Any:
    """Minimal ``CommandObject`` stand-in — only ``.command`` is read."""
    return type("Cmd", (), {"command": word})()


def _bot(username: str | None) -> Any:
    """Stand-in whose ``me()`` answers with the given username.

    It deliberately implements ``me`` and *not* ``get_me``: the former
    memoises on the Bot instance and the latter always hits the API, so
    a handler that slipped back to ``get_me`` would fail here with an
    ``AttributeError`` instead of quietly costing a round-trip per
    mistyped command.
    """

    class FakeBot:
        async def me(self) -> Any:
            return type("Me", (), {"username": username})()

    return FakeBot()


async def _noop(**_kwargs: Any) -> None:
    """A worker handler; the wrapper never calls it, only reads filters."""


# ── the reply ──────────────────────────────────────────────────────


@pytest.mark.parametrize("word", ["shop", "магазин", "topup", "vip", "чек"])
async def test_echoes_the_alias_typed(word: str) -> None:
    message = FakeMessage()
    await _refuse(message, _command(word), "ru", _bot("kom17bot"))
    text, _ = message.answers[0]
    assert text == t("h_private_only_command", "ru", command=word)
    assert f"/{word}" in text


async def test_answers_in_the_callers_language() -> None:
    ru, en = FakeMessage(), FakeMessage()
    await _refuse(ru, _command("shop"), "ru", _bot("kom17bot"))
    await _refuse(en, _command("shop"), "en", _bot("kom17bot"))
    assert ru.answers[0][0] != en.answers[0][0]
    assert "в личке" in ru.answers[0][0]
    assert "private chat" in en.answers[0][0]


async def test_escapes_html_in_the_alias() -> None:
    """Same defence in depth as the group-only twin: the alias renders
    into an HTML-parsed message."""
    message = FakeMessage()
    await _refuse(message, _command("<b>x</b>"), "ru", _bot("kom17bot"))
    assert "&lt;b&gt;x&lt;/b&gt;" in message.answers[0][0]
    assert "<b>x</b>" not in message.answers[0][0]


async def test_attaches_a_deep_link_button_to_the_bot() -> None:
    """The refusal is a navigation instruction, so it carries the way
    to follow it — a user told "go to a DM" should not have to search
    for the bot.

    #1926: and the link names the group they were standing in, so the
    DM opens knowing what the interrupted task was about.
    """
    message = FakeMessage()
    await _refuse(message, _command("shop"), "ru", _bot("some_bot"))
    _, markup = message.answers[0]
    button = markup.inline_keyboard[0][0]
    assert button.url == f"https://t.me/some_bot?start=grp_{_GROUP_ID}"
    assert button.text == t("h_private_only_btn", "ru")


@pytest.mark.parametrize("username", [None, "", "   "])
async def test_falls_back_when_telegram_reports_no_username(username: str | None) -> None:
    """A blank username would otherwise produce ``https://t.me/?start``
    — a button that goes nowhere is worse than the plain refusal."""
    message = FakeMessage()
    await _refuse(message, _command("shop"), "ru", _bot(username))
    _, markup = message.answers[0]
    assert markup.inline_keyboard[0][0].url == f"https://t.me/kom17bot?start=grp_{_GROUP_ID}"


# ── the wrapper ────────────────────────────────────────────────────


def _refusal_commands(wrapper: Router) -> dict[tuple[bool, str], set[str]]:
    """``{(ignore_case, prefix): words}`` registered by the refusal half."""
    refusal = wrapper.sub_routers[-1]
    out: dict[tuple[bool, str], set[str]] = {}
    for handler in refusal.message.handlers:
        for flt in handler.filters or ():
            if isinstance(flt.callback, Command):
                key = (flt.callback.ignore_case, flt.callback.prefix)
                out.setdefault(key, set()).update(
                    c for c in flt.callback.commands if isinstance(c, str)
                )
    return out


def test_harvests_the_workers_words_including_sub_routers() -> None:
    """The refusal list is read off the worker so the two cannot drift.
    A word added to a child router must be covered as well — several
    modules split their families that way."""
    worker = Router(name="w")
    worker.message.register(_noop, Command("shop", "магазин", ignore_case=True))
    child = Router(name="w-child")
    child.message.register(_noop, Command("buy", ignore_case=True))
    worker.include_router(child)

    wrapper = with_chat_type_refusal(worker, scope="private")
    assert _refusal_commands(wrapper) == {(True, "/"): {"shop", "магазин", "buy"}}


def test_keeps_the_workers_matching_style_per_group() -> None:
    """A case-sensitive registration must not gain a case-insensitive
    refusal: ``/SHOP`` would then be answered by a bot that would never
    have run it."""
    worker = Router(name="w")
    worker.message.register(_noop, Command("shop", ignore_case=True))
    worker.message.register(_noop, Command("topup", ignore_case=False))
    worker.message.register(_noop, Command("vip", prefix="!"))

    specs = _refusal_commands(with_chat_type_refusal(worker, scope="private"))
    assert specs[(True, "/")] == {"shop"}
    assert specs[(False, "/")] == {"topup"}
    assert specs[(False, "!")] == {"vip"}


def test_skips_owner_tier_words() -> None:
    """``/broadcast`` in a group must stay silent — a refusal tells a
    stranger the command exists. The tier is read from the catalog, so
    promoting a command changes this in the same edit."""
    worker = Router(name="w")
    worker.message.register(_noop, Command("shop", "broadcast", ignore_case=True))

    assert _refusal_commands(with_chat_type_refusal(worker, scope="private")) == {
        (True, "/"): {"shop"}
    }


def test_skips_words_another_module_serves_in_the_other_chat_type() -> None:
    """``/voice_settings`` is the group panel here and the VIP personal
    settings in a DM. Refusing either side would break the other."""
    worker = Router(name="w")
    worker.message.register(_noop, Command("voice_settings", ignore_case=True))
    assert with_chat_type_refusal(worker, scope="group") is worker


def test_skips_command_start() -> None:
    """``CommandStart`` subclasses ``Command`` with ``commands=("start",)``,
    so a deep-link entry point (``checks.py``'s ``check_`` link) would
    otherwise put ``/start`` in the refusal list and shadow the group
    welcome in ``handlers/start.py``."""
    worker = Router(name="w")
    worker.message.register(_noop, CommandStart(deep_link=True, magic=F.args))
    assert with_chat_type_refusal(worker, scope="private") is worker


def test_a_router_without_commands_is_returned_unchanged() -> None:
    """So callers can wrap unconditionally instead of deciding first."""
    worker = Router(name="w")
    worker.message.register(_noop, F.text)
    assert with_chat_type_refusal(worker, scope="private") is worker


def test_wrapper_carries_no_filter_of_its_own() -> None:
    """The whole reason for the wrapper: a router-level filter applies
    to every child, so anything on the wrapper would gate the worker —
    and a chat-type gate there would re-create the silence."""
    worker = Router(name="w")
    worker.message.register(_noop, Command("shop"))
    wrapper = with_chat_type_refusal(worker, scope="private")

    assert wrapper is not worker
    assert wrapper.message._handler.filters in (None, [])  # noqa: SLF001
    assert wrapper.sub_routers[0] is worker  # worker first: it wins its own chat type


@pytest.mark.parametrize(
    ("scope", "handler", "accepted", "refused"),
    [
        ("private", handle_private_only, ChatType.SUPERGROUP, ChatType.PRIVATE),
        ("group", handle_group_only, ChatType.PRIVATE, ChatType.SUPERGROUP),
    ],
)
def test_refusal_answers_only_the_other_chat_type(
    scope: Any, handler: Any, accepted: ChatType, refused: ChatType
) -> None:
    """The refusal must fire where the worker does *not*, and nowhere
    else — resolved through the registered magic filter rather than
    read off the source."""
    worker = Router(name="w")
    worker.message.register(_noop, Command("shop"))
    refusal = with_chat_type_refusal(worker, scope=scope).sub_routers[-1]

    registered = refusal.message.handlers[0]
    assert registered.callback is handler

    def _stub(chat_type: ChatType) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(type=chat_type.value),
            from_user=SimpleNamespace(id=1),
        )

    # Find the gate by what it does, not by its spelling: exactly one
    # registered magic filter may change its answer with the chat type.
    gates: list[MagicFilter] = []
    for flt in registered.filters or ():
        magic = getattr(flt, "magic", None)
        if not isinstance(magic, MagicFilter):
            continue
        if bool(magic.resolve(_stub(accepted))) != bool(magic.resolve(_stub(refused))):
            gates.append(magic)

    assert len(gates) == 1
    assert gates[0].resolve(_stub(accepted))
    assert not gates[0].resolve(_stub(refused))


def test_silence_rule_reads_the_catalog_not_a_hand_list() -> None:
    """A guard on the guard: if ``_SILENT_FROM_RANK`` were lowered to
    the ordinary tier every wrapped module would go quiet again, and
    every assertion above would still pass."""
    assert chat_scope._SILENT_FROM_RANK > 0  # noqa: SLF001
    assert not chat_scope._stays_silent("shop")  # noqa: SLF001
    assert chat_scope._stays_silent("broadcast")  # noqa: SLF001
