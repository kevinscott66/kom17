"""``requirements.txt`` must stay the application's dependency set.

The file drifted for the whole length of the strangler migration: it
still listed what the monolith needed — flask, matplotlib,
faster-whisper, requests, python-dotenv — and named none of aiogram,
fastapi, SQLAlchemy, Alembic or dishka. Nothing noticed, because the
service installs from ``uv.lock`` and the file has no other consumer
inside the repo. A reader following the README's ``pip install -r
requirements.txt`` therefore built an environment the application
cannot import, and found out at the first ``python -m``.

``pyproject.toml`` is the declaration and ``uv.lock`` is the pin;
``requirements.txt`` is a convenience copy for a plain-pip environment.
A copy with no test is a copy that goes stale, so this pins the one
property that matters: every declared runtime dependency is present,
with the same version range.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: ``extra`` markers and case differ freely between the two files
#: (``pyTelegramBotAPI`` vs ``pytelegrambotapi``); PEP 503 says they
#: name the same project, so compare on the normalised form.
_NORMALISE = re.compile(r"[-_.]+")


def _key(requirement: str) -> str:
    """Normalised ``name[extras]`` of one requirement line."""
    name = re.split(r"[<>=!~;\s]", requirement, maxsplit=1)[0]
    return _NORMALISE.sub("-", name).lower()


def _specifier(requirement: str) -> str:
    """The version range of one requirement, whitespace removed."""
    head, _, tail = requirement.partition(";")
    match = re.search(r"[<>=!~]", head)
    return "" if match is None else head[match.start() :].replace(" ", "")


def _declared() -> dict[str, str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps: list[str] = data["project"]["dependencies"]
    return {_key(d): _specifier(d) for d in deps}


def _pinned() -> dict[str, str]:
    lines = (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    active = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return {_key(ln): _specifier(ln) for ln in active}


def test_every_declared_dependency_is_installable_with_pip() -> None:
    declared, pinned = _declared(), _pinned()
    missing = sorted(set(declared) - set(pinned))
    assert not missing, (
        f"requirements.txt is missing {missing} — `pip install -r requirements.txt` "
        "would produce an environment the application cannot import"
    )


def test_requirements_does_not_invent_dependencies() -> None:
    """An uncommented line with no counterpart in pyproject is drift.

    Optional extras — the payment SDKs, the monolith's own libraries —
    belong in the commented sections, where they document the choice
    without being installed by it.
    """
    declared, pinned = _declared(), _pinned()
    extra = sorted(set(pinned) - set(declared))
    assert not extra, (
        f"requirements.txt installs {extra}, which pyproject does not declare — "
        "move them to a commented section or add them to [project.dependencies]"
    )


def test_version_ranges_agree() -> None:
    declared, pinned = _declared(), _pinned()
    disagree = {
        name: (declared[name], pinned[name])
        for name in set(declared) & set(pinned)
        if declared[name] != pinned[name]
    }
    assert not disagree, f"version ranges differ between the two files: {disagree}"
