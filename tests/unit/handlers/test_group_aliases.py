"""Unit tests for per-group dynamic command aliases (L-60, cluster H3).

Covered:

* ``normalize_target`` — slash stripping, lower-casing, Bot-API command
  grammar enforcement, first-token-only coercion (legacy bot.py:42121-42124).
* ``GroupAliasMiddleware`` — rewrites a group message whose first token
  is an alias word into ``/command rest`` (legacy args pass-through,
  bot.py:43623), passes everything else through unchanged, never
  consumes the update, skips private chats / slash commands / bot
  senders / FSM-stateful chats, and swallows map-read failures.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from aiogram.enums import ChatType
from aiogram.types import Message

from telegram_invite_bot.handlers.group_aliases import (
    GroupAliasMiddleware,
    normalize_target,
)
from telegram_invite_bot.repositories.group_aliases_repo import normalize_alias_word

_CHAT = -100123


# ---------------------------------------------------------------------------
# normalize_alias_word (legacy normalize_alias_token, bot.py:41951-41953)
# ---------------------------------------------------------------------------


def test_normalize_word_lowercases_and_strips_punctuation() -> None:
    assert normalize_alias_word("  Бал-ланс!  ") == "балланс"


def test_normalize_word_keeps_latin_digits_underscore() -> None:
    assert normalize_alias_word("Top_10") == "top_10"


def test_normalize_word_empty_for_pure_punctuation() -> None:
    assert normalize_alias_word("?!.") == ""


# ---------------------------------------------------------------------------
# normalize_target
# ---------------------------------------------------------------------------


def test_target_accepts_bare_and_slashed() -> None:
    assert normalize_target("balance") == "balance"
    assert normalize_target("/balance") == "balance"


def test_target_lowercases() -> None:
    assert normalize_target("/Balance") == "balance"


def test_target_takes_first_token_only() -> None:
    # Legacy: command_str.split()[0] (bot.py:42124).
    assert normalize_target("/top args ignored") == "top"


def test_target_rejects_cyrillic_and_specials() -> None:
    assert normalize_target("/баланс") is None
    assert normalize_target("/foo-bar") is None
    assert normalize_target("") is None


def test_target_rejects_overlong() -> None:
    assert normalize_target("a" * 33) is None
    assert normalize_target("a" * 32) == "a" * 32


# ---------------------------------------------------------------------------
# GroupAliasMiddleware
# ---------------------------------------------------------------------------


def _msg(
    text: str | None,
    chat_type: ChatType = ChatType.SUPERGROUP,
    *,
    is_bot: bool = False,
) -> Message:
    return Message.model_construct(
        message_id=777,
        date=cast("Any", None),
        chat=cast("Any", SimpleNamespace(id=_CHAT, type=chat_type)),
        from_user=cast("Any", SimpleNamespace(id=42, is_bot=is_bot)),
        text=text,
    )


def _middleware(mapping: dict[str, str], *, raise_on_read: bool = False) -> GroupAliasMiddleware:
    mw = GroupAliasMiddleware(registry=cast("Any", None))

    async def _fake_mapping_for(group_id: int) -> dict[str, str]:
        if raise_on_read:
            raise RuntimeError("db unavailable")
        return mapping

    # Bypass the DB read — the cache/repo path is covered separately.
    mw._mapping_for = _fake_mapping_for  # type: ignore[method-assign]
    return mw


async def _run(
    mw: GroupAliasMiddleware, event: Message, data: dict[str, Any] | None = None
) -> Message:
    seen: dict[str, Any] = {}

    async def _handler(ev: Any, d: dict[str, Any]) -> str:
        seen["event"] = ev
        return "ok"

    result = await mw(_handler, event, data or {})
    assert result == "ok"  # never consumes the update
    return cast("Message", seen["event"])


async def test_rewrites_exact_alias_word() -> None:
    mw = _middleware({"казик": "roulette"})
    out = await _run(mw, _msg("казик"))
    assert out.text == "/roulette"


async def test_passes_args_through() -> None:
    # Legacy: message.text = f"{command} {rest}" (bot.py:43623).
    mw = _middleware({"казик": "roulette"})
    out = await _run(mw, _msg("Казик 100"))
    assert out.text == "/roulette 100"


async def test_normalizes_trigger_word() -> None:
    mw = _middleware({"казик": "roulette"})
    out = await _run(mw, _msg("КаЗиК!"))
    assert out.text == "/roulette"


async def test_unknown_word_untouched() -> None:
    mw = _middleware({"казик": "roulette"})
    msg = _msg("привет всем")
    out = await _run(mw, msg)
    assert out is msg


async def test_slash_command_untouched() -> None:
    mw = _middleware({"казик": "roulette"})
    msg = _msg("/казик")
    out = await _run(mw, msg)
    assert out is msg


async def test_private_chat_untouched() -> None:
    mw = _middleware({"казик": "roulette"})
    msg = _msg("казик", chat_type=ChatType.PRIVATE)
    out = await _run(mw, msg)
    assert out is msg


async def test_bot_sender_untouched() -> None:
    mw = _middleware({"казик": "roulette"})
    msg = _msg("казик", is_bot=True)
    out = await _run(mw, msg)
    assert out is msg


async def test_non_text_untouched() -> None:
    mw = _middleware({"казик": "roulette"})
    msg = _msg(None)
    out = await _run(mw, msg)
    assert out is msg


async def test_fsm_state_blocks_rewrite() -> None:
    mw = _middleware({"казик": "roulette"})

    class _State:
        async def get_state(self) -> str:
            return "some:state"

    msg = _msg("казик")
    out = await _run(mw, msg, {"state": _State()})
    assert out is msg


async def test_map_read_failure_passes_through() -> None:
    mw = _middleware({}, raise_on_read=True)
    msg = _msg("казик")
    out = await _run(mw, msg)  # must not raise
    assert out is msg


async def test_a_resolved_raw_state_is_trusted_without_a_storage_read() -> None:
    """#1558: the guard's answer is already in ``data`` — do not re-read it.

    aiogram's ``FSMContextMiddleware`` is an update-level OUTER
    middleware and resolves ``raw_state`` before any router middleware
    runs (``aiogram/fsm/middleware.py:42``). This middleware awaited
    ``state.get_state()`` unconditionally, which under the
    ``FSM_BACKEND=sqlite`` production runs is a database read on every
    plain message in every group — for a value already parsed. #1039
    made that fix in ``TextAliasMiddleware``; this is the same fix on the
    twin that sits beside it.

    ``_ExplodingState`` is what makes this an assertion rather than a
    performance note: it turns the redundant read back into a failure.
    """

    class _ExplodingState:
        async def get_state(self) -> str | None:
            raise AssertionError("raw_state was in data; the storage must not be read")

    mw = _middleware({"казик": "roulette"})

    # Mid-FSM-step: the alias must not hijack the answer the user is
    # typing into /support or /withdraw.
    blocked = _msg("казик")
    data: dict[str, Any] = {"raw_state": "SupportForm:text", "state": _ExplodingState()}
    assert await _run(mw, blocked, data) is blocked

    # Not in a step: the alias resolves as usual, still without a read.
    idle: dict[str, Any] = {"raw_state": None, "state": _ExplodingState()}
    assert (await _run(mw, _msg("казик"), idle)).text == "/roulette"


async def test_a_data_dict_without_raw_state_still_asks_the_context() -> None:
    """The fallback is not decoration: skipping the guard would be a bug.

    A hand-built ``data`` — a test, a bare dispatcher, a future wiring
    that attaches this middleware above the FSM one — can carry the
    context without the resolved key. Treating a missing key as "no
    state" would silently let an alias hijack an in-progress FSM step,
    so the read still happens exactly there and nowhere else.
    """

    class _CountingState:
        def __init__(self, value: str | None) -> None:
            self._value = value
            self.reads = 0

        async def get_state(self) -> str | None:
            self.reads += 1
            return self._value

    mw = _middleware({"казик": "roulette"})

    busy = _CountingState("SupportForm:text")
    blocked = _msg("казик")
    assert await _run(mw, blocked, {"state": busy}) is blocked
    assert busy.reads == 1

    free = _CountingState(None)
    assert (await _run(mw, _msg("казик"), {"state": free})).text == "/roulette"
    assert free.reads == 1
