"""#1944: every anti-abuse stamp must be committed inside its own lock.

``GameLimitService.record`` is a bare ``session.add``
(``repositories/game_limits_repo.py``), so the stamp stays invisible to
every other connection until something commits it. :data:`PLAY_LOCKS`
serialises one user's ``check -> play -> record``, but the lock releases
when the ``async with`` block ends and the middleware commits later —
so the next update from the same player takes the lock, reads
``game_plays`` without the row, and is waved through. That is #222-B,
and it is closed one call site at a time by committing INSIDE the lock,
immediately after ``record``.

Which makes those ``await checkpoint()`` calls load-bearing, and their
failure mode invisible: delete one as redundant — the natural reading
of a lock that already "serialises" the sequence — and the caps simply
stop biting for a user who fires two updates a few milliseconds apart.
Nothing raises, nothing logs, and no existing test notices, because
every one of them plays a single game at a time.

``games/limits.py`` used to guard that with prose: a three-entry
inventory of the call sites, by line number. There are nine, and the
three numbers it did name had all drifted. An inventory that has to be
maintained by hand is one that documents the tree as it was; this test
derives it instead.

The rule is structural rather than proximity-based, for the same reason
``test_money_call_sites`` gave up on ±5 lines: a ``record`` passes iff
an ``await checkpoint()`` appears later in the SAME ``async with``
block. Line numbers cannot drift out of that, and a ``checkpoint()``
that a refactor moved out of the lock stops counting — which is exactly
the regression, since a commit after the release is what the middleware
was already doing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_HANDLERS = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot" / "handlers"

#: The stamp under guard. Receiver-scoped: an unrelated ``.record(...)``
#: on some other service is not an anti-abuse stamp.
_RECEIVERS = {"game_limit_service", "game_limits", "_game_limit_service"}


def _is_stamp(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _RECEIVERS
    )


def _is_checkpoint(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "checkpoint"
    )


def _stamps(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if _is_stamp(n)]


def _committed_stamps(tree: ast.AST) -> set[int]:
    """Line numbers of stamps followed by a ``checkpoint()`` in their lock."""
    committed: set[int] = set()
    for block in ast.walk(tree):
        if not isinstance(block, ast.AsyncWith):
            continue
        inner = list(ast.walk(block))
        checkpoints = [n.lineno for n in inner if _is_checkpoint(n)]
        for stamp in (n for n in inner if _is_stamp(n)):
            if any(line > stamp.lineno for line in checkpoints):
                committed.add(stamp.lineno)
    return committed


def _sources() -> list[Path]:
    return sorted(p for p in _HANDLERS.glob("*.py") if p.name != "__init__.py")


def _sites() -> list[tuple[Path, int, bool]]:
    """``(file, line, committed)`` for every anti-abuse stamp in handlers."""
    found: list[tuple[Path, int, bool]] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        committed = _committed_stamps(tree)
        found.extend((path, call.lineno, call.lineno in committed) for call in _stamps(tree))
    return found


def test_every_play_stamp_is_committed_inside_its_lock() -> None:
    """The guard proper. One uncommitted stamp reopens #222-B."""
    offenders = [f"{p.name}:{line}" for p, line, ok in _sites() if not ok]

    assert offenders == [], (
        "these ``game_limit_service.record`` calls are not followed by an "
        "``await checkpoint()`` inside the same lock, so the stamp stays "
        "uncommitted until the middleware gets round to it and the next "
        "update from the same player is waved through (#222-B): "
        f"{offenders}"
    )


def test_the_stamps_are_actually_found() -> None:
    """The control: a guard that matches nothing passes vacuously.

    The receiver set above is a whitelist, so an injection renamed in a
    refactor would silently empty this test rather than fail it. Pinning
    a floor means the rename shows up here instead.
    """
    sites = _sites()

    assert len(sites) >= 9
    assert {p.name for p, _, _ in sites} >= {
        "games.py",
        "roulette.py",
        "pvp_stake.py",
        "duel.py",
        "rps.py",
    }


@pytest.mark.parametrize("name", ["games.py", "roulette.py", "pvp_stake.py", "duel.py", "rps.py"])
def test_the_detector_sees_an_uncommitted_stamp(name: str) -> None:
    """The discrimination check, per file, on the real source.

    Neutering the ``checkpoint()`` is the edit this test exists to
    catch, so it is performed here — on a parsed copy, never on disk —
    and the guard must go red for it. Without this, a detector that
    quietly stopped matching would keep reporting a clean tree forever.

    The call is replaced rather than deleted: every one of them sits
    under ``if checkpoint is not None:``, so removing the line would
    leave an empty block and fail on a syntax error instead of on the
    thing being tested.
    """
    source = (_HANDLERS / name).read_text(encoding="utf-8")
    tree = ast.parse(source.replace("await checkpoint()", "pass"))

    assert _stamps(tree), f"{name} has no stamp to strip — update the inventory"
    assert _committed_stamps(tree) == set()
