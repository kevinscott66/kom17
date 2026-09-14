"""#2026: one group's word list cannot make every other chat wait.

Every message in every group the bot serves is scanned with a single
alternation compiled from that group's banned words
(``WordFilterAutomodMiddleware._compile``, wordfilter.py:465). The scan
is synchronous and runs on the one shared event loop, and its cost is
roughly ``len(text) x len(pattern)`` — so the pattern's total size is a
global latency budget, not a per-group one.

The count ceiling alone did not bound it. ``validate_word`` admits
multi-word phrases up to 100 characters on purpose (a phrase entry is
the point of the feature), so 500 entries could be 81 kB of pattern;
against a 4 096-character message built from the same repeated token the
scan measured **187 ms**, roughly five messages a second to stall the
whole bot. The attacker needs one throwaway group, where they are the
creator and so pass every admin gate, and one ordinary member account
to post from. ``AntifloodMiddleware`` does not help: the automod
middleware is registered ahead of it (routers/main_router.py:543-547),
so the regex runs before throttling is consulted.

The fix is a cap on the *combined* length of the list
(``MAX_FILTER_TOTAL_CHARS``), checked on the way in by both writers.
4 000 characters costs ~15 ms in the same worst case and still admits
500 ordinary words, so the limit an admin actually meets is still the
count one.

What this file does NOT check, deliberately: a wall-clock assertion.
Timing a regex on CI hardware would either be flaky or so loose as to
prove nothing. The property that makes the timing safe is the pattern
size, and that is exact — so that is what gets asserted.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from telegram_invite_bot.handlers import wordfilter as wf
from telegram_invite_bot.handlers.wordfilter import (
    MAX_FILTER_TOTAL_CHARS,
    MAX_WORD_LENGTH,
    MAX_WORDS_PER_GROUP,
    add_refusal,
)

_SRC = Path(wf.__file__).resolve().parents[1]

#: One entry of the shape the attack uses: maximal length, mostly a
#: repeated token so that most branches of the alternation match deep
#: into the text before failing.
_PHRASE = ("а " * ((MAX_WORD_LENGTH - 2) // 2)) + "zz"


class _Message:
    def __init__(self, text: str) -> None:
        self.chat = SimpleNamespace(id=-100500, type="supergroup")
        self.from_user = SimpleNamespace(id=42, username="admin", is_bot=False)
        self.text = text
        self.caption = None
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _Repo:
    def __init__(self, words: list[str]) -> None:
        self._words = words
        self.calls: list[str] = []

    async def list(self, *, group_id: int) -> list[str]:  # noqa: ARG002
        self.calls.append("list")
        return list(self._words)

    async def add(self, **_kw: Any) -> bool:  # noqa: ANN401
        self.calls.append("add")
        return True


@pytest.fixture(autouse=True)
def _admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler gates on a live Telegram admin probe; short it out."""

    async def _yes(*_a: object, **_kw: object) -> bool:
        return True

    async def _ru(*_a: object, **_kw: object) -> str:
        return "ru"

    monkeypatch.setattr(wf, "_require_admin", _yes)
    monkeypatch.setattr(wf, "_resolve_lang", _ru)


def _fill_to_budget() -> list[str]:
    """As many maximal phrases as the budget admits, and no more."""
    words: list[str] = []
    while add_refusal(words, _PHRASE) is None:
        words.append(f"{_PHRASE[: -len(str(len(words))) or None]}{len(words)}")
    return words


def test_a_full_list_of_maximal_phrases_stays_inside_the_budget() -> None:
    """The ceiling that actually bounds the scan is the character one.

    Filling with the worst-admissible entry is the whole point: the
    count ceiling is never reached on this path, so a guard that only
    counted would let the list grow twenty times past this.
    """
    words = _fill_to_budget()
    total = sum(len(w) for w in words)
    assert total <= MAX_FILTER_TOTAL_CHARS
    assert len(words) < MAX_WORDS_PER_GROUP, (
        "the phrases were not long enough to reach the character budget first — "
        "this case would then prove nothing about the pattern size"
    )
    assert add_refusal(words, _PHRASE) == "h_wf_limit_chars"


def test_the_compiled_pattern_is_bounded_by_the_budget() -> None:
    """Size is the thing the latency follows from, so pin the size.

    The escaping and the two lookarounds add a constant per entry, so
    the bound is the budget plus that overhead — not the budget itself.
    An entry costs at most ``(?<!\\w)`` + ``(?!\\w)`` + ``|`` = 14
    characters of scaffolding, and ``re.escape`` at most doubles the
    word. That ceiling is what keeps the worst case in milliseconds.
    """
    words = _fill_to_budget()
    pattern = wf.WordFilterAutomodMiddleware._compile(words)  # noqa: SLF001
    assert pattern is not None
    ceiling = 2 * MAX_FILTER_TOTAL_CHARS + 14 * len(words)
    assert len(pattern.pattern) <= ceiling, (
        f"pattern grew to {len(pattern.pattern)} characters against a {ceiling} ceiling"
    )


def test_ordinary_words_still_fit_five_hundred_times_over() -> None:
    """The cap must not be felt by the people it is not aimed at.

    A real banned word is a word, not a 100-character sentence; 500 of
    them at eight characters is 4 000, which is exactly the budget. If
    this ever fails, the limit an admin meets has changed from "too
    many words" to "too much text", and the copy they get is wrong.
    """
    words = ["мерзавец"] * (MAX_WORDS_PER_GROUP - 1)
    assert add_refusal(words, "мерзавец") is None


async def test_the_command_refuses_the_oversized_phrase_without_writing() -> None:
    """End to end: the refusal reaches the admin and nothing is stored."""
    repo = _Repo(_fill_to_budget())
    message = _Message(f"/filter_add {_PHRASE}")

    await wf.handle_filter_add(
        cast("Any", message),
        cast("Any", None),
        cast("Any", repo),
        cast("Any", None),
        cast("Any", None),
        None,
    )

    assert repo.calls == ["list"], f"the oversized phrase was written: {repo.calls}"
    assert len(message.replies) == 1


def test_both_writers_go_through_the_same_gate() -> None:
    """The panel is a second front door; it must use the same lock.

    ``/groupadmin``'s Words panel adds words with its own session and
    its own copy of the flow. When it enforced the count itself it was
    one edit away from drifting; the guard is that neither writer names
    a ceiling constant directly any more.
    """
    offenders: list[str] = []
    for name in ("wordfilter.py", "groupadmin.py"):
        path = _SRC / "handlers" / name
        tree = ast.parse(path.read_text())
        # Everything except the one function that is *supposed* to name
        # them — it is the gate, not a second opinion.
        gates = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "add_refusal"
        ]
        exempt = {id(n) for gate in gates for n in ast.walk(gate)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or id(node) in exempt:
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"MAX_WORDS_PER_GROUP", "MAX_FILTER_TOTAL_CHARS"}:
                offenders.append(f"{name}:{node.lineno}")

    assert not offenders, (
        "a ceiling is compared outside add_refusal, so the two writers can "
        f"now disagree about what a full list is: {offenders}"
    )
