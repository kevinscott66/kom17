"""Every ``/command`` the bot *advertises* must be a command it answers.

``tests/regression/test_help_surface.py`` already pins the ``/help``
catalog against the router. That covers the one surface built from a
structured catalog — but most command names reach the user from
free-text copy instead:

* the ``h_*`` / ``site_*`` values in ``i18n/data/*.yaml`` — cards that
  say "type /ask", "see /mode", "example: /ask explain /send";
* the ``_SECTIONS`` catalog in ``handlers/admin/panel.py`` — one
  editorial line per ``/admin_*`` command, written by hand;
* ``telegraph_guide_{ru,en}.md`` at the repo root — the prose guide the
  site renders at ``/commands`` beside the generated chip index. #116
  found 18 dead names there while both guards above were green: the
  YAML scan never opened the files, and the chip index is built from
  ``core/ranks`` and so cannot see what the prose claims.

A command name is not the only thing copy promises, though. Some
triggers have no slash at all — «ком вопрос», "ai, question" — and the
guards above are blind to them by construction: their pattern requires
a leading ``/``. That blind spot is #206. The English FAQ told its
reader to type ``"kom question"``, which matched nothing whatsoever
(the extractor knew only the Cyrillic «ком»), and ``"ai question"``,
which resolved in a DM and silently did nothing in the group the
sentence is about. Both had been wrong since the string was written.
``test_copy_only_promises_triggers_that_resolve`` closes it.

Nothing checked those. The defect this file was written for: the AI
help card offered ``/ask Объясни команду /gift`` in both locales, but
``/gift`` was never ported — legacy's transfer command is ``/send``
in the new pipeline. A user who followed the example asked the model
to explain a command that does not exist, and the model — having no
catalog — happily invented one.

Scope of the scan, and why it is deliberately narrow:

* **Only lowercase ASCII names.** Russian aliases (``/погода``) are
  registered too, but a Cyrillic ``/word`` in prose is nearly always
  ordinary text, and the false positives would drown the signal. The
  Latin half is where the examples live.
* **Only ``h_*`` / ``site_*`` keys.** The rest of the YAML is a
  verbatim carry-over of legacy ``translations.py`` (pinned by
  ``tests/unit/i18n/test_legacy_parity.py``) — much of it unreferenced
  by the new pipeline, and none of it ours to re-word.
* ``</code>``, ``{avg}/day`` and ``/proc/meminfo`` are excluded by the
  pattern itself rather than by an allowlist, so a genuine dead command
  can never be silently absorbed into a list of "known exceptions".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from telegram_invite_bot.core.ranks import COMMAND_ALIAS_TO_KEY
from telegram_invite_bot.handlers.admin.panel import _SECTIONS
from telegram_invite_bot.handlers.ai import extract_ai_direct_question
from telegram_invite_bot.i18n import _load
from telegram_invite_bot.middlewares.text_alias import (
    _ALIAS_MAP,
    BARE_GROUP_PHRASES,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_YAML_DIR = _ROOT / "src/telegram_invite_bot/i18n/data"

# Leading guard: not preceded by a word char (``RUB/COM``), another
# slash (``/proc/meminfo``), ``<`` (``</code>``) or ``}`` (``{avg}/day``).
# Trailing guard: not followed by a word char, ``*`` (``admin_*`` prose)
# or ``/`` (a filesystem path).
_SLASH_COMMAND = re.compile(r"(?<![\w/<}])/([a-z][a-z0-9_]{1,30})(?![\w*/])")

_OUR_KEY = re.compile(r"^(h_[a-z0-9_]+|site_[a-z0-9_]+):")


def _advertised_in_yaml(locale: str) -> Iterator[tuple[str, str, int]]:
    """Yield ``(command, key, line_no)`` for our own copy in one locale."""
    path = _YAML_DIR / f"{locale}.yaml"
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        key = _OUR_KEY.match(line)
        if key is None:
            continue
        for match in _SLASH_COMMAND.finditer(line):
            yield match.group(1), key.group(1), line_no


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_copy_only_advertises_registered_commands(
    locale: str,
    registered_commands: set[str],
) -> None:
    """A card that names a command the router never claimed is a dead end.

    The user taps the auto-linked name, the bot answers nothing, and
    there is no error to diagnose — the update simply falls through.
    """
    dead = [
        f"{key} (line {line_no}) advertises /{command}"
        for command, key, line_no in _advertised_in_yaml(locale)
        if command not in registered_commands
    ]
    assert not dead, f"{locale}.yaml advertises unregistered commands:\n" + "\n".join(dead)


def _advertised_in_guide(locale: str) -> Iterator[tuple[str, int]]:
    """Yield ``(command, line_no)`` for the prose guide of one locale.

    ``**`` and backticks are stripped before the shared pattern runs.
    Everything in these files is bold — ``**/cpc_accept**`` — and the
    pattern's trailing ``(?!\\*)`` guard (there to skip ``admin_*``
    prose) would otherwise treat the closing emphasis as a wildcard and
    wave the name through. That is exactly how the first scan of these
    files reported four dead names instead of twenty.
    """
    path = _ROOT / f"telegraph_guide_{locale}.md"
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.replace("**", " ").replace("`", " ")
        for match in _SLASH_COMMAND.finditer(line):
            yield match.group(1), line_no


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_guide_only_advertises_registered_commands(
    locale: str,
    registered_commands: set[str],
) -> None:
    """The prose guide is the *other* half of the ``/commands`` page.

    Same failure as the YAML scan, one surface over: a name in the guide
    is auto-linked by Telegram and tappable on the site, so a stale one
    sends the reader to silence. Note the guide must therefore never
    write a command it is *denying* the existence of with a leading
    slash — say "no ``kom_`` aliases", not "no ``/kom_roulette``", or
    this guard cannot tell the denial from a promise.
    """
    dead = sorted(
        {
            f"/{command} (line {line_no})"
            for command, line_no in _advertised_in_guide(locale)
            if command not in registered_commands
        }
    )
    assert not dead, f"telegraph_guide_{locale}.md advertises unregistered commands:\n" + "\n".join(
        dead
    )


_CYRILLIC_RUN = re.compile(r"[\u0400-\u04FF]+(?: [\u0400-\u04FF]+)*")


def test_the_english_guide_is_written_in_english() -> None:
    """The blind spot the scan above admits to, closed for one file.

    ``_SLASH_COMMAND`` matches lowercase ASCII only, so a Cyrillic name
    in the prose is invisible to every guard in this module — which is
    how the English guide came to offer ``/кнб``, ``/очистить``,
    ``/репорт`` and ``/вывод`` as the alternatives to their own
    canonical spellings. They are real, registered triggers, so the
    dead-name scan had nothing to say; they are simply unusable by the
    reader they were printed for, who cannot type them (#176).

    The rule is one-directional on purpose. The Russian guide is *meant*
    to carry both spellings, and the generated chip index already drops
    Cyrillic from the English page on its own; this covers the hand-
    written half that no filter runs over.
    """
    text = (_ROOT / "telegraph_guide_en.md").read_text(encoding="utf-8")
    assert not _CYRILLIC_RUN.findall(text)


def test_the_russian_guide_still_carries_russian() -> None:
    """Proof the check above is aimed at a file that could fail it.

    A guard that reads the wrong path, or a pattern that matches
    nothing, passes just as quietly as a clean file would.
    """
    text = (_ROOT / "telegraph_guide_ru.md").read_text(encoding="utf-8")
    assert _CYRILLIC_RUN.findall(text)


# ``**alive**`` and ``` `настройки` `` — the two ways the guides emphasise
# a single word. Multi-word spans (``**bot balance**``) are skipped by the
# ``\S`` class: those are the prefixed group phrases, not bare aliases.
_EMPHASISED_WORD = re.compile(r"\*\*([^*\s]+)\*\*|`([^`\s]+)`")

# ``**h**``, ``**m**``, ``**d**``, ``**w**`` are the duration suffixes in the
# ``/mute`` and ``/ban`` lines. ``h`` also happens to be a ``/help`` alias,
# which is the only reason this set has to exist.
_DURATION_UNITS = frozenset({"m", "h", "d", "w", "s"})


def _bare_typeable() -> frozenset[str]:
    """Words the bot answers when they arrive with no leading slash."""
    return frozenset(_ALIAS_MAP) | frozenset(BARE_GROUP_PHRASES)


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_guides_print_aliases_the_reader_can_actually_type(locale: str) -> None:
    """An alias printed without its slash is a promise the bot won't keep.

    The guides list alternatives as ``**/relationship** (или **rel**,
    **отношения**)`` — the canonical name slashed, the aliases bare. Read
    literally that says both alternatives are typed as written, and for
    one of them it is true: ``отношения`` is in ``_ALIAS_MAP``, so the
    bare-word middleware resolves it in a DM. ``rel`` is not in the map,
    so typed exactly as the guide prints it, it does nothing — in any
    chat. Two words, rendered identically, behaving differently.

    Slash-less dispatch is far narrower than the alias table: a bare
    word resolves only through ``_ALIAS_MAP`` (private chats) or the
    short ``BARE_GROUP_PHRASES`` whitelist (groups). Every other alias
    needs the slash. So the guard is not "always print a slash" — it is
    "print a slash unless the middleware would genuinely take the word
    without one", which keeps ``бот баланс`` and ``.дейли`` in RU:161
    legal while catching the eleven spots that were not (#178).
    """
    path = _ROOT / f"telegraph_guide_{locale}.md"
    bare = _bare_typeable()
    unusable = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in _EMPHASISED_WORD.finditer(line):
            word = (match.group(1) or match.group(2)).strip().lower()
            if word.startswith(("/", ".", "?", "!")) or word in _DURATION_UNITS:
                continue
            if word in COMMAND_ALIAS_TO_KEY and word not in bare:
                key = COMMAND_ALIAS_TO_KEY[word]
                unusable.append(f"line {line_no}: «{word}» — write /{word} (of /{key})")

    assert not unusable, (
        f"telegraph_guide_{locale}.md prints aliases without the slash they "
        "need, and the bare-word middleware does not accept them:\n" + "\n".join(unusable)
    )


def test_admin_panel_catalog_only_lists_registered_commands(
    registered_commands: set[str],
) -> None:
    """The ``/admin`` catalog is hand-maintained, one line per command.

    Its whole purpose is to be the operator's index of what exists, so a
    stale row is worse here than in ordinary copy: the reader trusts it
    precisely because it looks exhaustive.
    """
    dead = [
        f"section {key!r} lists {cmd}"
        for key, _label, _header, cmds in _SECTIONS
        for cmd, _desc in cmds
        if cmd.startswith("/") and cmd.lstrip("/") not in registered_commands
    ]
    assert not dead, "/admin catalog lists unregistered commands:\n" + "\n".join(dead)


#: How the product spells its assistant, in every locale it ships. A
#: quoted example in our copy that *opens* with one of these is a
#: promise that typing the phrase reaches the assistant — which is the
#: only thing the guard below asserts. The list is curated rather than
#: derived, so ``test_every_assistant_spelling_is_a_real_trigger``
#: pins each entry against the extractor: an invented spelling here
#: would otherwise weaken the guard silently instead of failing it.
_ASSISTANT_NAMES = frozenset({"ии", "ком", "kom", "ai"})

# ``"..."`` in the English copy, ``«...»`` in the Russian. Both files
# use their own convention consistently, so the pattern carries both
# rather than guessing per locale.
_QUOTED = re.compile(r'"([^"\n]{1,120})"|«([^»\n]{1,120})»')

_FIRST_WORD = re.compile(r"[\s,:]+")


def _quoted_examples(locale: str) -> Iterator[tuple[str, str]]:
    """Yield ``(key, phrase)`` for quoted spans in copy we own."""
    for key, value in _load(locale).items():
        if not key.startswith(("h_", "site_")) or not isinstance(value, str):
            continue
        for match in _QUOTED.finditer(value):
            phrase = (match.group(1) or match.group(2)).strip()
            if phrase:
                yield key, phrase


def test_every_assistant_spelling_is_a_real_trigger() -> None:
    """Proof the vocabulary below is aimed at words that could fail.

    ``_ASSISTANT_NAMES`` is hand-written. If a name in it stopped being
    a trigger — or was never one — the guard that uses it would quietly
    stop covering every phrase that opens with it, and pass.
    """
    dead = sorted(n for n in _ASSISTANT_NAMES if extract_ai_direct_question(n) is None)
    assert not dead, f"_ASSISTANT_NAMES lists non-triggers: {dead}"


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_copy_only_promises_triggers_that_resolve(locale: str) -> None:
    """A quoted example must work in the chat the sentence describes.

    The bar is ``is_group=True`` deliberately, and it is the strict
    half: the space form of ``"ai"`` is refused in groups precisely
    because English sentences open with the word. None of our cards
    scope their examples to DMs — the FAQ line says "In chat" and then
    gives one example for every chat there is — so an example that only
    works in a DM is exactly the #206 defect, not an exemption from it.
    A future DM-only card would have to say so in its own copy, and
    would need a carve-out here to prove it.
    """
    broken = [
        f"{key}: «{phrase}» reaches nothing"
        for key, phrase in _quoted_examples(locale)
        if _FIRST_WORD.split(phrase.lower())[0] in _ASSISTANT_NAMES
        and extract_ai_direct_question(phrase, is_group=True) is None
    ]
    assert not broken, (
        f"{locale}.yaml quotes assistant triggers that do not resolve:\n" + "\n".join(broken)
    )
