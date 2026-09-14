"""Plain-text command-alias router (A-06).

Legacy ``process_text_shortcut_command`` answered bare/prefixed Russian
words (``меню``, ``баланс``, ``бот`` …) as shortcuts for the matching
``/slash`` command. The bridge deletion dropped that surface; the
:class:`TextAliasMiddleware` restores it by rewriting recognised text to
the canonical ``/command`` and letting the normal routers dispatch.

Pins the gating that prevents stealing ordinary group chatter:

* Private DM → any bare alias word resolves.
* Group → only prefixed (``.``/``?``/``!``/``бот ``) or the tiny bare
  whitelist (``меню``, ``кто я``, ``пинг``, ``бот`` …) resolves; every
  other bare word passes through untouched.
* Unported targets (``/chatinfo`` …) and non-alias words → passthrough.
"""

from __future__ import annotations

import ast
import pathlib
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares import text_alias
from telegram_invite_bot.middlewares.text_alias import (
    BARE_GROUP_PHRASES,
    GROUP_PREFIXES,
    TextAliasMiddleware,
    _resolve,
    plain_triggers_by_command,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


# ---- pure gating logic ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "chat_type", "expected"),
    [
        # Private: bare alias words resolve.
        ("бот", "private", "/botcheck"),
        ("баланс", "private", "/balance"),
        ("топ", "private", "/top"),
        ("профиль", "private", "/profile"),
        ("погода Москва", "private", "/weather Москва"),
        ("меню", "private", "/help"),
        ("кто я", "private", "/profile"),
        # Private: unknown word / unported target → no rewrite.
        ("привет всем", "private", None),
        ("чат инфа", "private", "/chatinfo"),  # CMD-2: /chatinfo now ported
        # Group: bare non-whitelist word must NOT resolve (no chatter theft).
        ("баланс", "supergroup", None),
        ("топ", "supergroup", None),
        ("профиль", "supergroup", None),
        # Group: bare whitelist phrases resolve.
        ("меню", "supergroup", "/help"),
        ("пинг", "supergroup", "/ping"),
        ("бот", "supergroup", "/botcheck"),
        ("кто я", "supergroup", "/profile"),
        # Group: prefixed invocation resolves for any alias.
        (".баланс", "supergroup", "/balance"),
        ("?топ", "supergroup", "/top"),
        ("бот профиль", "supergroup", "/profile"),
        ("!погода Казань", "supergroup", "/weather Казань"),
        # Already a slash command → never touched.
        ("/balance", "private", None),
        # AI prefixes are reserved for the AI handler, not the alias map.
        ("ком привет", "private", None),
        ("ии вопрос", "supergroup", None),
        # I18N-3: bilingual triggers for the session's new commands.
        # Private bare resolves in both languages.
        ("цитата", "private", "/quote"),
        ("quote", "private", "/quote"),
        ("шутка", "private", "/joke"),
        ("joke", "private", "/joke"),
        ("крипта", "private", "/crypto"),
        ("crypto", "private", "/crypto"),
        ("валюта", "private", "/currency"),
        ("currency", "private", "/currency"),
        ("rate USD", "private", "/rate USD"),
        ("курс USD", "private", "/rate USD"),
        ("прогноз Москва", "private", "/forecast Москва"),
        ("forecast London", "private", "/forecast London"),
        ("roulette 100", "private", "/roulette 100"),
        ("рулетка 100", "private", "/roulette 100"),
        ("achievements", "private", "/achievements"),
        ("достижения", "private", "/achievements"),
        # шутка18 still maps to /joke18, not the SFW /joke.
        ("шутка18", "private", "/joke18"),
        # Group: bare new-command word must NOT resolve (no chatter theft);
        # only the prefixed form does.
        ("crypto", "supergroup", None),
        ("рулетка", "supergroup", None),
        (".крипта", "supergroup", "/crypto"),
        ("?прогноз Сочи", "supergroup", "/forecast Сочи"),
        # RR-6 #68: a weather QUESTION (no alias word first) is handed to
        # /weather intact — the handler parses the city and the period.
        ("какая погода в Казани", "private", "/weather какая погода в Казани"),
        ("что по погоде завтра", "private", "/weather что по погоде завтра"),
        ("скажи погоду в Магадане", "private", "/weather скажи погоду в Магадане"),
        ("what's the weather in Berlin", "private", "/weather what's the weather in Berlin"),
        # Same gating as every other alias: a bare group question is left
        # alone, a prefixed one resolves.
        ("какая погода в Казани", "supergroup", None),
        ("бот какая погода в Казани", "supergroup", "/weather какая погода в Казани"),
        # AI-prefixed weather questions stay with Kom, in both chat types.
        ("ком какая погода в Москве", "private", None),
        ("ии какая погода", "private", None),
        (".ком какая погода в Москве", "supergroup", None),
        # Not a weather question — still passthrough.
        ("погоди немного", "private", None),
    ],
)
def test_resolve(text: str, chat_type: str, expected: str | None) -> None:
    assert _resolve(text, chat_type) == expected


# ---- the FSM guard reads what aiogram already resolved ----------------


class _ExplodingState:
    """A context whose storage read is itself the assertion."""

    async def get_state(self) -> str | None:
        raise AssertionError("raw_state was already in data; the storage must not be read")


class _CountingState:
    """A context that answers, and records that it was asked."""

    def __init__(self, value: str | None) -> None:
        self._value = value
        self.reads = 0

    async def get_state(self) -> str | None:
        self.reads += 1
        return self._value


def _group_message(text: str) -> Any:
    message = make_message_update(text, chat_type="supergroup", chat_id=-100777).message
    assert message is not None
    return message


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        # Mid-FSM-step: the alias must not hijack the answer the user is
        # typing into /support or /withdraw.
        ("SupportForm:text", None),
        # Not in a step: the alias resolves as usual.
        (None, "/crypto"),
    ],
)
async def test_a_resolved_raw_state_is_trusted_without_a_storage_read(
    raw_state: str | None, expected: str | None
) -> None:
    """#1039: ``raw_state`` is already in ``data`` — do not read it again.

    aiogram's ``FSMContextMiddleware`` is an update-level OUTER
    middleware and resolves the state before any router middleware runs
    (``aiogram/fsm/middleware.py:42``), so the guard's answer is in hand
    the moment this middleware is entered. The old code awaited
    ``state.get_state()`` unconditionally, which under the
    ``FSM_BACKEND=sqlite`` production runs is a database read on every
    plain text message in every group — for a value already parsed.

    ``_ExplodingState`` is what makes this a real assertion rather than a
    performance note: it turns the redundant read back into a failure.
    """
    middleware = TextAliasMiddleware()

    rewritten = await middleware._maybe_rewrite(  # noqa: SLF001
        _group_message(".крипта"), {"raw_state": raw_state, "state": _ExplodingState()}
    )

    assert (None if rewritten is None else rewritten.text) == expected


async def test_a_data_dict_without_raw_state_still_asks_the_context() -> None:
    """The fallback is not decoration: skipping the guard would be a bug.

    A hand-built ``data`` — a test, a bare dispatcher, a future wiring
    that attaches this middleware above the FSM one — can carry the
    context without the resolved key. Treating a missing key as "no
    state" would silently let an alias hijack an in-progress FSM step,
    so the read still happens exactly there and nowhere else.
    """
    middleware = TextAliasMiddleware()
    state = _CountingState("SupportForm:text")

    assert await middleware._maybe_rewrite(_group_message(".крипта"), {"state": state}) is None  # noqa: SLF001
    assert state.reads == 1

    # And with no context at all the guard simply does not apply.
    idle = _CountingState(None)
    rewritten = await middleware._maybe_rewrite(  # noqa: SLF001
        _group_message(".крипта"), {"state": idle}
    )
    assert rewritten is not None
    assert rewritten.text == "/crypto"
    assert idle.reads == 1


# ---- end-to-end wiring ------------------------------------------------


async def test_private_bare_alias_dispatches(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Private bare ``бот`` is rewritten to ``/botcheck`` and answered."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("бот", chat_type="private", user_id=7)
    )
    assert result is not UNHANDLED
    assert sent[-1]["text"] == t("h_botcheck", "ru")


async def test_group_prefixed_alias_dispatches(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Group ``.бот`` (prefixed) resolves; bare group words would not."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update(".бот", chat_type="supergroup", chat_id=-100, user_id=7)
    )
    assert result is not UNHANDLED
    assert sent[-1]["text"] == t("h_botcheck", "ru")


async def test_group_whitelist_bare_dispatches(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``бот`` is in the legacy un-prefixed group whitelist."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("бот", chat_type="supergroup", chat_id=-100, user_id=7)
    )
    assert result is not UNHANDLED
    assert sent[-1]["text"] == t("h_botcheck", "ru")


async def test_group_bare_nonwhitelist_passes_through(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A bare non-whitelist alias word in a group must NOT be hijacked —
    regression guard against stealing ordinary chatter."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("баланс", chat_type="supergroup", chat_id=-100, user_id=7)
    )
    # No command handler claims bare "баланс" in a group → UNHANDLED, and
    # certainly no /botcheck/balance reply.
    assert result is UNHANDLED
    assert sent == []


async def test_plain_group_chatter_untouched(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "ребята как дела сегодня", chat_type="supergroup", chat_id=-100, user_id=7
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# ---- the constants the public guide prints ----------------------------


@pytest.mark.parametrize("phrase", BARE_GROUP_PHRASES)
def test_advertised_bare_phrases_resolve_in_a_group(phrase: str) -> None:
    """``BARE_GROUP_PHRASES`` is documentation with teeth.

    The site prints it as "these work with nothing in front, in a
    group". A phrase that quietly stopped resolving — a target that was
    never ported, a normalisation that changed — would turn the page
    into a list of things that do nothing, and nobody would notice from
    reading the middleware.
    """
    assert _resolve(phrase, "supergroup") is not None


#: Spellings the un-prefixed group path accepts that the public list
#: deliberately does not print. Each is a punctuation or vowel variant
#: of a phrase that IS printed, and spelling every one of them out on
#: the page would turn a five-line rule into a spelling table. The
#: allowlist is small on purpose: a genuinely new trigger word has to
#: be advertised, not added here.
_UNADVERTISED_VARIANTS = frozenset(
    {
        "ктоя",  # «кто я» written closed up
        "чатинфа",  # «чат инфа» written closed up
        "чат инфо",  # «инфо» in place of «инфа»
        "чатинфо",  # both at once
        "chatinfo",  # «chat info» written closed up
    }
)


def _bare_group_literals() -> frozenset[str]:
    """Every string the un-prefixed group path compares its input against.

    Read out of the module's own source rather than retyped here: a
    hand-copied list is exactly the thing that drifts, and drift is what
    the caller exists to catch. Both functions are scanned because the
    phrase fold runs before the group branch, so a spelling it folds is
    accepted bare in a group just as surely as one the branch lists.
    """
    tree = ast.parse(pathlib.Path(text_alias.__file__).read_text(encoding="utf-8"))
    wanted = {"_extract_payload", "_fold_phrase"}
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
            continue
        for compare in ast.walk(node):
            if not isinstance(compare, ast.Compare):
                continue
            if not any(isinstance(op, ast.In) for op in compare.ops):
                continue
            for operand in compare.comparators:
                if not isinstance(operand, ast.Tuple):
                    continue
                literals.update(
                    elt.value
                    for elt in operand.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
    return frozenset(literals)


def test_every_bare_word_the_matcher_takes_in_a_group_is_advertised() -> None:
    """The direction the sibling above does not cover.

    ``test_advertised_bare_phrases_resolve_in_a_group`` walks the public
    list and checks the matcher honours it — it cannot see a word the
    matcher takes that the list never mentions. «команда» was exactly
    that for as long as the guard ran one way only: the branch accepted
    it, ``_ALIAS_MAP`` mapped it to ``/help``, and the page said nothing
    about it, so the only way to find it was to read the source.

    An unadvertised trigger is not a bug on its own; it is a promise
    nobody made, which is why the fix is to print it rather than to
    delete it. What this asserts is that the choice gets made — either
    the word goes on the page or it goes in the variant allowlist with
    a reason beside it.
    """
    accepted = _bare_group_literals()
    # If the scan itself broke, everything below passes vacuously.
    assert accepted, "no literals found — the ast scan is broken, not the matcher"

    unadvertised = accepted - set(BARE_GROUP_PHRASES) - _UNADVERTISED_VARIANTS
    assert unadvertised == set(), unadvertised

    # And the allowlist may not outlive the spellings it excuses.
    assert accepted >= _UNADVERTISED_VARIANTS


@pytest.mark.parametrize("prefix", GROUP_PREFIXES)
def test_advertised_prefixes_reach_a_command_in_a_group(prefix: str) -> None:
    assert _resolve(f"{prefix}баланс", "supergroup") == "/balance"


def test_plain_triggers_are_the_map_read_backwards() -> None:
    """Every advertised trigger has to resolve to the command it is
    advertised under — the inversion is what the website prints.
    """
    for command, words in plain_triggers_by_command().items():
        for word in words:
            assert _resolve(word, "private") == f"/{command}", (word, command)


@pytest.mark.parametrize("prefix", GROUP_PREFIXES)
@pytest.mark.parametrize("phrase", BARE_GROUP_PHRASES)
def test_an_advertised_prefix_works_on_an_advertised_phrase(prefix: str, phrase: str) -> None:
    """The two rules the site prints have to hold together, not apart.

    The public index states them side by side — a prefix makes *any*
    trigger fire, and these phrases need no prefix at all. A reader who
    combines them writes «бот кто я», the most natural way to address a
    bot in a group. That resolved to nothing until #171: the prefix
    branch returned the payload before the multi-word fold could run,
    so every two-word phrase died the moment someone was polite enough
    to address the bot first.
    """
    assert _resolve(f"{prefix}{phrase}", "supergroup") is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("бот кто я", "/profile"),
        ("!кто я", "/profile"),
        (". кто я", "/profile"),
        ("бот me", "/profile"),
        ("бот чат инфа", "/chatinfo"),
        (".чат инфо", "/chatinfo"),
        ("?chat info", "/chatinfo"),
    ],
)
def test_prefixed_multiword_phrases_reach_their_command(text: str, expected: str) -> None:
    assert _resolve(text, "supergroup") == expected


@pytest.mark.parametrize("text", ["кто я вообще такой", "!кто я такой", "бот чат инфа за неделю"])
def test_a_sentence_that_merely_starts_like_a_phrase_is_not_a_command(text: str) -> None:
    """The fold is whole-phrase only.

    «кто я вообще такой» is someone talking, and the prefixed form is
    someone talking *to the bot* — but neither is ``/profile``. Folding
    on a prefix match would turn every question that opens with these
    words into a card.
    """
    assert _resolve(text, "supergroup") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("city Kazan", "/city Kazan"),
        ("chatinfo", "/chatinfo"),
    ],
)
def test_the_last_two_commands_without_a_latin_trigger_have_one(text: str, expected: str) -> None:
    """I18N-3: every plain-text command reachable in Russian is now also
    reachable in English.

    ``/city`` had none at all, and ``/chatinfo`` only reached English
    through the phrase fold — which the site's per-command trigger list
    cannot see, so its row advertised Cyrillic to an English reader.
    """
    assert _resolve(text, "private") == expected


def test_every_plain_command_advertises_a_latin_trigger() -> None:
    """The guard that keeps I18N-3 closed.

    Adding a Russian-only trigger for a new command is the easy
    mistake — it works for the author, and the gap only shows up on the
    English site, which nobody re-reads.
    """
    ru_only = [
        command
        for command, words in plain_triggers_by_command().items()
        if not any(word.isascii() for word in words)
    ]
    assert ru_only == []
