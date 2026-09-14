"""Regression guard: no ordinary command may be silent in a chat (#123).

#122 fixed the per-registration half of this — a command gated with
``F.chat.type.in_(GROUP_TYPES)`` and nothing else never *refuses* a
private invocation, it simply never matches one, and the user hears
nothing. ``test_chat_type_twins`` locks that half in by reading the
source.

The other half can't be read from the source at all: roughly 180
commands take their gate from ``router.message.filter(...)``, which
applies to every handler in the router *and every child router* — so
the answer to "which chat types can this handler match?" lives in the
assembled tree, not in any one ``register(...)`` call. Hence this suite
builds the production dispatcher and asks it directly.

**How the question is asked.** Each handler is resolved against two
stub messages that differ only in ``chat.type``. A filter counts as
chat-type-sensitive only when flipping the stub flips its verdict —
every other predicate (``F.from_user``, ``F.args.is_(None)``, a state
filter) answers identically both times and is ignored rather than
guessed at. That keeps the audit honest about what it does not know:
a filter it cannot evaluate at all is skipped, never assumed to pass.

The one router this audit has to look past is the ``#158`` tail. It
carries no chat-type filter by design — it is the last thing aiogram
tries, every refusal twin is included ahead of it, and it answers only
the argument forms nothing else claimed. In this map that makes it a
co-owner of every word in *both* chat types, which would read as a
collision it can never actually cause. ``scope_map`` therefore strips
it and ``raw_scope_map`` keeps it, so the exclusion is checked against
the real tree instead of being assumed.

Two rules come out of it:

* a refusal must never shadow a real handler — if ``/shop`` answers in
  a group with "this only works in a DM", no other handler may be
  claiming ``/shop`` in a group;
* every command that is still one-sided must have a reason to be.
  ``handlers/admin/*`` and the owner-tier words are the reason:
  answering a group ``/admin_cpu`` would confirm the command exists to
  whoever typed it. Everything else must speak.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.filters import Command
from magic_filter import MagicFilter

from telegram_invite_bot.core.ranks import RankLevel, command_key_for, default_min_rank

if TYPE_CHECKING:
    from aiogram import Dispatcher

PRIVATE = "private"
GROUP = "supergroup"

#: The two handlers whose entire job is to say "wrong chat type".
REFUSALS = {
    "telegram_invite_bot.handlers.group_only.handle_group_only",
    "telegram_invite_bot.handlers.chat_scope.handle_private_only",
}

#: Modules allowed to stay silent regardless of rank — the whole
#: developer console. See the module docstring.
SILENT_PACKAGE = "telegram_invite_bot.handlers.admin."

#: The #158 tail. Not a worker and not a refusal: it answers "that
#: argument form matched nothing" for words ``/help`` can describe, and
#: it is included last precisely so it can never preempt either. Its
#: own surface — which words it names, and that it really is last — is
#: guarded by ``test_unknown_form_surface.py``; here it is only noise,
#: and this suite subtracts it before asking who owns a word.
TAIL = {"telegram_invite_bot.handlers.unknown_form.handle_unknown_form"}


def _stub(chat_type: str) -> SimpleNamespace:
    """A message-shaped object carrying only what a filter may read.

    Deliberately not a real :class:`~aiogram.types.Message`: the point
    is to vary one attribute and hold everything else fixed, and a
    validated model would force plausible values for fields no filter
    in this tree looks at.
    """
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=1, is_bot=False),
        text="/x",
        args=None,
        content_type="text",
        reply_to_message=None,
    )


def _verdict(probe: Any, chat_type: str) -> bool | None:
    """What one filter says about a stub, or ``None`` if it can't say.

    Async filters (``Command``, ``StateFilter``) answer with a
    coroutine, which is closed rather than awaited: neither depends on
    ``chat.type``, so running them would cost an event loop to learn
    nothing. Filters that raise on the stub are equally unknowable.
    """
    try:
        answer = probe(_stub(chat_type))
    except Exception:  # noqa: BLE001 - a stub cannot satisfy every filter
        return None
    close = getattr(answer, "close", None)
    if inspect.isawaitable(answer):
        if callable(close):
            close()
        return None
    return bool(answer)


def _accepts(filters: list[Any], chat_type: str) -> bool:
    """Can a handler carrying ``filters`` match a message of that type?"""
    other = GROUP if chat_type == PRIVATE else PRIVATE
    for flt in filters:
        # aiogram's FilterObject replaces a MagicFilter callback with
        # its bound ``resolve`` and keeps the original on ``.magic``,
        # so the magic expression has to be read from there. Anything
        # else (a plain lambda gate, a Command, a StateFilter) is
        # called directly.
        magic = getattr(flt, "magic", None)
        probe = magic.resolve if isinstance(magic, MagicFilter) else getattr(flt, "callback", flt)
        here = _verdict(probe, chat_type)
        there = _verdict(probe, other)
        if here is None or there is None or here == there:
            continue  # not chat-type-sensitive: says nothing either way
        if not here:
            return False
    return True


def _scope_map(dispatcher: Dispatcher) -> dict[str, dict[str, set[str]]]:
    """``{command word: {chat type: {owning handler, …}}}``.

    Router-level filters are carried down the walk because that is
    exactly what aiogram does: a child router is only reached once its
    parents' root filters have passed.
    """
    served: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def _walk(router: Any, inherited: list[Any]) -> None:
        observer = getattr(router, "message", None)
        if observer is None:
            for sub in getattr(router, "sub_routers", []):
                _walk(sub, inherited)
            return
        here = [*inherited, *(observer._handler.filters or [])]  # noqa: SLF001
        for handler in observer.handlers:
            fn = handler.callback
            owner = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
            words = [
                token
                for flt in handler.filters or []
                if isinstance(getattr(flt, "callback", None), Command)
                for token in flt.callback.commands
                if isinstance(token, str)
            ]
            if not words:
                continue
            combined = [*here, *(handler.filters or [])]
            for chat_type in (PRIVATE, GROUP):
                if _accepts(combined, chat_type):
                    for word in words:
                        served[word][chat_type].add(owner)
        for sub in getattr(router, "sub_routers", []):
            _walk(sub, here)

    _walk(dispatcher, [])
    return served


@pytest.fixture
def raw_scope_map(production_dispatcher: Dispatcher) -> dict[str, dict[str, set[str]]]:
    """The tree as it is, tail included. Only the guard-the-guard reads it."""
    return _scope_map(production_dispatcher)


@pytest.fixture
def scope_map(
    raw_scope_map: dict[str, dict[str, set[str]]],
) -> dict[str, dict[str, set[str]]]:
    """The same map with the #158 tail subtracted.

    A chat type whose only owner was the tail is dropped rather than
    left empty: "this word answers in one chat type only" is the
    question :func:`test_every_still_silent_command_is_deliberately_silent`
    asks, and a tail-only entry would silently answer it "no" for every
    describable command in the bot.
    """
    stripped: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for word, by_type in raw_scope_map.items():
        for chat_type, owners in by_type.items():
            workers = owners - TAIL
            if workers:
                stripped[word][chat_type] = workers
    return stripped


def test_scan_sees_both_sides_of_a_wrapped_command(
    scope_map: dict[str, dict[str, set[str]]],
    raw_scope_map: dict[str, dict[str, set[str]]],
) -> None:
    """Guard the guard: an audit that matches nothing passes forever.

    ``/shop`` is private-only and ``/ban`` group-only, and both are
    gated at the router level — the exact shape this suite exists for.
    Each must show a real handler on its own side and a refusal on the
    other.

    The tail is checked here too, from the other direction: the raw map
    must still contain it on the away side. An exclusion nobody proves
    is live is how an audit quietly stops auditing — if the tail is
    ever renamed or dropped, this fails instead of ``TAIL`` becoming a
    set that subtracts nothing.
    """
    for word, home in (("shop", PRIVATE), ("ban", GROUP)):
        away = GROUP if home == PRIVATE else PRIVATE
        assert scope_map[word][home] - REFUSALS, word
        assert scope_map[word][away] <= REFUSALS, word
        assert scope_map[word][away], word
        assert raw_scope_map[word][away] >= TAIL, word


def test_no_refusal_shadows_a_real_handler(
    scope_map: dict[str, dict[str, set[str]]],
) -> None:
    """A refusal that fires where the command actually works is worse
    than the silence it replaced: the command stops functioning and the
    bot explains, wrongly, that it never worked here.
    """
    collisions = sorted(
        f"/{word} in {chat_type}: {sorted(owners)}"
        for word, by_type in scope_map.items()
        for chat_type, owners in by_type.items()
        if (owners & REFUSALS) and (owners - REFUSALS)
    )
    assert collisions == [], (
        "a chat-type refusal is registered for the same word and chat "
        "type as a working handler:\n  " + "\n  ".join(collisions)
    )


def test_every_still_silent_command_is_deliberately_silent(
    scope_map: dict[str, dict[str, set[str]]],
) -> None:
    """Anything one-sided must be a developer command or owner-tier.

    Both exemptions are about not advertising: a stranger typing
    ``/admin_disk`` or ``/broadcast`` in a group learns nothing from
    silence and learns the command exists from a refusal.
    """
    offenders = []
    for word, by_type in sorted(scope_map.items()):
        if len(by_type) != 1:
            continue
        chat_type, owners = next(iter(by_type.items()))
        if owners & REFUSALS:
            continue  # a lone refusal is the mirror of a missing worker
        if all(owner.startswith(SILENT_PACKAGE) for owner in owners):
            continue
        if default_min_rank(command_key_for(word)) >= RankLevel.OWNER:
            continue
        offenders.append(f"/{word} answers only in {chat_type} ({sorted(owners)})")
    assert offenders == [], (
        "these commands say nothing at all in the other chat type — wrap "
        "their router with handlers.chat_scope.with_chat_type_refusal, or "
        "register a refusal twin:\n  " + "\n  ".join(offenders)
    )
