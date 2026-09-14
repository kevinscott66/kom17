"""#1940: an atomic write that fails must not leave its temp file behind.

Two places in the tree do the same tmp-file-plus-``os.replace`` dance,
and both passed ``delete=False`` to :class:`tempfile.NamedTemporaryFile`
without a ``finally``:

* :meth:`JsonFileEditorBridge._write` — the guide site's overrides file,
  written on every operator save;
* :func:`command_access._write_snapshot` — the /cmdcfg kill-switch
  snapshot, written on every rank change.

``delete=False`` is correct: the file's lifetime has to survive the
``with`` so ``os.replace`` can consume it. What was missing is the other
half of that bargain — when the rename never happens, nobody deletes it.
Every failed save left one ``<name>.<random>.tmp`` next to the real file,
permanently, in ``database/``: a directory an operator never lists and
that is excluded from the repo, so the growth is invisible until the
volume fills.

Both failure points are covered, because they fail for different
reasons: a serialisation error inside the ``with`` (the payload is bad)
and a rename error after it (the filesystem said no). The success path
is pinned too — the fix must not start deleting the file the rename
already consumed, which would be a far worse bug than the one it cures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from telegram_invite_bot.cms.guide_site import editor as editor_mod
from telegram_invite_bot.cms.guide_site.editor import JsonFileEditorBridge
from telegram_invite_bot.handlers import command_access as ca_mod

if TYPE_CHECKING:
    from collections.abc import Callable


def _tmp_leftovers(directory: Path) -> list[str]:
    """Every stray temp file both writers would have created."""
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def _explode(*_args: Any, **_kwargs: Any) -> Any:
    """Stand-in for whatever goes wrong halfway through a save."""
    raise OSError("no space left on device")


# ── The guide editor ─────────────────────────────────────────────────────────


def test_guide_editor_leaves_no_tmp_when_serialisation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure happens INSIDE the ``with``, before the rename."""
    target = tmp_path / "settings.json"
    target.write_text('{"kept": 1}', encoding="utf-8")
    monkeypatch.setattr(editor_mod.json, "dump", _explode)

    with pytest.raises(OSError, match="no space left"):
        JsonFileEditorBridge(target)._write({"guide_ru": "x"})  # noqa: SLF001

    assert _tmp_leftovers(tmp_path) == []
    # The original is untouched — the whole point of writing aside.
    assert json.loads(target.read_text(encoding="utf-8")) == {"kept": 1}


def test_guide_editor_leaves_no_tmp_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure happens AFTER the ``with`` closed the temp file."""
    target = tmp_path / "settings.json"
    monkeypatch.setattr(editor_mod.os, "replace", _explode)

    with pytest.raises(OSError, match="no space left"):
        JsonFileEditorBridge(target)._write({"guide_ru": "x"})  # noqa: SLF001

    assert _tmp_leftovers(tmp_path) == []


def test_guide_editor_repeated_failures_do_not_accumulate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the leak's actual shape.

    One orphan is untidy; the defect is that they PILE UP, one per
    attempt, because each temp file gets a fresh random suffix and so
    never overwrites the last one.
    """
    target = tmp_path / "settings.json"
    monkeypatch.setattr(editor_mod.os, "replace", _explode)
    bridge = JsonFileEditorBridge(target)

    for _ in range(5):
        with pytest.raises(OSError, match="no space left"):
            bridge._write({"guide_ru": "x"})  # noqa: SLF001

    assert _tmp_leftovers(tmp_path) == []


def test_guide_editor_success_still_writes_the_file(tmp_path: Path) -> None:
    """The fix must not delete what the rename already consumed."""
    target = tmp_path / "settings.json"

    JsonFileEditorBridge(target)._write({"guide_ru": "привет"})  # noqa: SLF001

    assert json.loads(target.read_text(encoding="utf-8")) == {"guide_ru": "привет"}
    assert _tmp_leftovers(tmp_path) == []


# ── The /cmdcfg snapshot ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attr", "owner"),
    [("dump", "json"), ("replace", "os")],
    ids=["serialisation", "rename"],
)
def test_command_access_snapshot_leaves_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attr: str, owner: str
) -> None:
    """Same two failure points on the kill-switch snapshot writer."""
    target = tmp_path / "cmdcfg.json"
    module: Any = {"json": ca_mod.json, "os": ca_mod.os}[owner]
    monkeypatch.setattr(module, attr, _explode)

    with pytest.raises(OSError, match="no space left"):
        ca_mod._write_snapshot(target, {"balance": 3})  # noqa: SLF001

    assert _tmp_leftovers(tmp_path) == []


def test_command_access_snapshot_success_round_trips(tmp_path: Path) -> None:
    """The success path, so the cleanup cannot silently eat a save."""
    target = tmp_path / "nested" / "cmdcfg.json"

    ca_mod._write_snapshot(target, {"balance": 3})  # noqa: SLF001

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["overrides"] == {"balance": 3}
    assert _tmp_leftovers(target.parent) == []


# ── The shape, so a third writer cannot reintroduce it ───────────────────────


@pytest.mark.parametrize(
    "writer",
    [JsonFileEditorBridge._write, ca_mod._write_snapshot],  # noqa: SLF001
    ids=["guide_editor", "cmdcfg_snapshot"],
)
def test_every_delete_false_writer_has_a_finally(writer: Callable[..., None]) -> None:
    """``delete=False`` and no ``finally`` is the bug, spelled out.

    A behavioural test cannot see a writer that does not exist yet.
    This one at least holds the two known ones to the pattern, so a
    later edit that drops the cleanup fails here with the reason
    attached rather than silently going back to leaking.
    """
    import inspect  # noqa: PLC0415 — only this assertion needs it

    source = inspect.getsource(writer)
    assert "delete=False" in source, "the pattern changed — revisit this guard"
    assert "finally:" in source
    assert "unlink(missing_ok=True)" in source
