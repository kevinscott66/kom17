"""Regression guard: nothing that pays out coins draws from Mersenne Twister.

``random``'s default generator is MT19937. Its 19937-bit state is
recoverable from enough observed output, and it was never meant to
resist an adversary — the stdlib documentation says as much. Every game
in this bot used it:

* ``handlers.games._flip_side`` is the *single* coin source for both the
  free ``/flip`` and the staked ``/flip 500 орёл``, so the cheap surface
  and the paid surface consumed one stream.
* ``handlers.roulette._rng`` and ``handlers.duel._rng`` each owned a
  dedicated ``random.Random()``, which is worse rather than better: no
  other consumer interleaves unknown draws, so what an observer collects
  is consecutive output of one generator.
* ``DailyService`` rolled the bonus, ``CheckService`` rolled a random
  cheque's payout, ``inventory_use_planner`` rolled a luck item's coins,
  and ``games.pvp`` fell back to the module-level ``random`` when its
  caller passed no generator (which production never does).

The fix is one primitive —
:data:`~telegram_invite_bot.utils.rng.money_rng`, a
:class:`random.SystemRandom` — and this file keeps it in place from
three angles: the primitive's own properties, the real wiring of the
three surfaces that decide a coin, and an AST scan so a new money module
cannot quietly reach for ``random`` again.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from telegram_invite_bot.handlers import duel as duel_handler
from telegram_invite_bot.handlers import games as games_handler
from telegram_invite_bot.handlers import roulette as roulette_handler
from telegram_invite_bot.utils.rng import money_rng

if TYPE_CHECKING:
    from collections.abc import Iterator

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


def test_money_rng_is_os_backed() -> None:
    assert isinstance(money_rng, random.SystemRandom)


def test_money_rng_still_satisfies_the_injection_signature() -> None:
    """Every game takes ``rng: random.Random``; the default must fit it.

    ``SystemRandom`` subclasses ``Random``, which is the whole reason
    the swap needed no signature change anywhere.
    """
    assert isinstance(money_rng, random.Random)


def test_money_rng_cannot_be_pinned_by_seeding() -> None:
    """Reproducibility is exactly the property being removed.

    ``SystemRandom.seed`` is a documented no-op, so two identically
    "seeded" runs still diverge. A 64-bit draw makes an accidental
    collision here about as likely as guessing the coin outright.
    """
    money_rng.seed(0)
    first = [money_rng.getrandbits(64) for _ in range(4)]
    money_rng.seed(0)
    second = [money_rng.getrandbits(64) for _ in range(4)]
    assert first != second


def test_money_rng_has_no_recoverable_state() -> None:
    """The state an attacker would want to reconstruct does not exist."""
    with pytest.raises(NotImplementedError):
        money_rng.getstate()


# ---------------------------------------------------------------------------
# The wiring — the three surfaces that turn a draw into coins
# ---------------------------------------------------------------------------


class _StubRng:
    """Minimal stand-in: returns one queued float per ``random()`` call."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self.value


@pytest.mark.parametrize(("draw", "expected"), [(0.1, "орёл"), (0.9, "решка")])
def test_flip_side_draws_from_money_rng(
    monkeypatch: pytest.MonkeyPatch, draw: float, expected: str
) -> None:
    """The coin must come from the module's ``money_rng`` name.

    Swapping that name changes the result, which is what proves the
    draw is taken there and not from some other generator.
    """
    stub = _StubRng(draw)
    monkeypatch.setattr(games_handler, "money_rng", stub)
    assert games_handler._flip_side() == expected
    assert stub.calls == 1


def test_duel_and_roulette_spin_the_shared_money_generator() -> None:
    """Both handlers hold module-level generators; both must be the OS one.

    ``is`` rather than ``isinstance``: a second ``SystemRandom()`` would
    be equally safe, but the guard is cheaper to reason about when there
    is exactly one money generator in the process.
    """
    assert duel_handler._rng is money_rng
    assert roulette_handler._rng is money_rng


# ---------------------------------------------------------------------------
# AST scan: no module may call into ``random`` unless it is cosmetic
# ---------------------------------------------------------------------------

#: Modules that legitimately keep the cheap generator. Each entry is a
#: claim that the draw decides nothing worth money and has no adversary
#: — not that the module is unimportant.
_COSMETIC: dict[str, str] = {
    "core/recent_picker.py": (
        "picks which pool entry to show so /joke and /quote don't repeat — "
        "no payout, and the pool is public anyway"
    ),
    "services/joke_service.py": ("picks which humour-API category and joke type to request"),
    "services/ai_context.py": ("rolls a decorative die into an AI prompt; nothing settles on it"),
    "utils/rng.py": "defines the money generator itself",
}


def _modules() -> Iterator[tuple[str, ast.AST]]:
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _annotation_node_ids(tree: ast.AST) -> set[int]:
    """Ids of every node sitting inside a type annotation.

    ``rng: random.Random | None = None`` names the *injection contract*
    — the parameter every game exposes so a test can pin a seeded
    generator. It is not a draw, and the modules that kept ``import
    random`` did so for exactly this. Flagging annotations would force a
    pointless rewrite of every game signature, so they are excised
    before the scan runs rather than special-cased inside it.
    """
    skip: set[int] = set()
    for node in ast.walk(tree):
        annotations: list[ast.expr | None] = []
        if isinstance(node, ast.AnnAssign | ast.arg):
            annotations.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotations.append(node.returns)
        for annotation in annotations:
            if annotation is not None:
                skip.update(id(sub) for sub in ast.walk(annotation))
    return skip


def _random_uses(tree: ast.AST) -> list[str]:
    """Runtime uses of the ``random`` module in ``tree``, outside annotations.

    Deliberately broader than "calls to ``random.foo()``". Three shapes
    reach the default generator without ever being such a call, and all
    three exist (or existed) in this codebase:

    * ``random.Random()`` — constructing a *private* Twister, which is
      the worst case rather than the safest one.
    * ``random_int_inclusive = random.randint`` — the bound method is
      stashed now and called somewhere else entirely, so the call node
      never mentions ``random``.
    * ``(rng or random).choice(...)`` — the module is used as a *value*
      standing in for a generator, so the attribute hangs off a
      ``BoolOp`` rather than off the name.

    ``from random import …`` is flagged on sight: it moves the names out
    of the ``random.`` namespace where nothing below could see them.
    """
    skip = _annotation_node_ids(tree)
    # A Name that is only there to carry an attribute (the ``random`` in
    # ``random.randint``) must not be reported a second time on its own.
    consumed = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "random"
    }

    uses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "random":
            uses.append(f"from random import … (line {node.lineno})")
            continue
        if id(node) in skip:
            continue
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "random"
        ):
            uses.append(f"random.{node.attr} (line {node.lineno})")
        elif (
            isinstance(node, ast.Name)
            and node.id == "random"
            and isinstance(node.ctx, ast.Load)
            and id(node) not in consumed
        ):
            uses.append(f"bare `random` as a value (line {node.lineno})")
    return uses


def _scan() -> tuple[dict[str, list[str]], int]:
    """Return ({module: uses}, number of modules parsed)."""
    found: dict[str, list[str]] = {}
    total = 0
    for rel, tree in _modules():
        total += 1
        uses = _random_uses(tree)
        if uses:
            found[rel] = uses
    return found, total


def test_no_money_module_draws_from_the_default_generator() -> None:
    found, total = _scan()
    # Non-vacuity: the walk has to have covered the package. A handful
    # of modules would mean the glob broke, not that the code is clean.
    assert total > 300, f"the scan parsed only {total} modules — the walk is broken"

    unexplained = {rel: uses for rel, uses in found.items() if rel not in _COSMETIC}
    assert not unexplained, (
        "these modules draw from random's default generator (MT19937), whose "
        "state is recoverable from observed output; if the draw decides coins "
        "route it through utils.rng.money_rng, otherwise add the module to "
        f"_COSMETIC with a reason: {unexplained}"
    )


def test_cosmetic_allowlist_has_no_stale_entries() -> None:
    """An entry that no longer uses ``random`` hides the next real one."""
    found, _ = _scan()
    stale = set(_COSMETIC) - set(found)
    assert not stale, (
        f"_COSMETIC lists {sorted(stale)}, but the scan finds no random use "
        f"there any more — drop the entry"
    )


# ---------------------------------------------------------------------------
# Guard the guard
# ---------------------------------------------------------------------------

#: Every shape that reaches the default generator, paired with the
#: marker the scan must emit for it. All four are drawn from real code:
#: the first two are what ``handlers/games.py`` and ``handlers/duel.py``
#: looked like before the fix, and the last two are the mutations that
#: an earlier, call-only version of this scan silently let through
#: (``inventory_use_planner`` and ``games/pvp.py``).
_OFFENDERS: dict[str, str] = {
    "a bare module-level draw": "return random.random() < 0.5",
    "a private Mersenne Twister": "_rng = random.Random()",
    "a stashed bound method": "roll = random.randint",
    "the module used as a generator value": "n = (rng or random).randint(1, 6)",
}

_FIXED = """
import random

from telegram_invite_bot.utils.rng import money_rng

_rng = money_rng
roll = money_rng.randint


def flip(*, rng: random.Random | None = None) -> random.Random:
    return (rng or money_rng).random() < 0.5


def take(source: random.Random) -> int:
    other: random.Random | None = None
    return (other or source).randint(1, 6)
"""

_SNEAKY = """
from random import randint


def payout():
    return randint(1, 100)
"""


@pytest.mark.parametrize(("shape", "source"), list(_OFFENDERS.items()))
def test_the_scan_catches_every_way_in(shape: str, source: str) -> None:
    assert _random_uses(ast.parse(source)), f"{shape} slipped past the scan"


def test_the_scan_clears_a_real_fix() -> None:
    """Including every surviving ``random.Random`` annotation — a scan
    that cannot tell a contract from a draw would just be turned off."""
    assert _random_uses(ast.parse(_FIXED)) == [], "a real fix reads as an offender"


def test_the_scan_sees_through_a_from_import() -> None:
    assert _random_uses(ast.parse(_SNEAKY)), "a from-import bypasses the scan"
