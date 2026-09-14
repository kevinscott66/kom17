"""``read_build_info`` — what the deploy claims it shipped.

The whole value of this module is that a *wrong* answer is worse than
no answer: an operator who sees a revision on ``/admin_status``
stops asking whether prod is stale. So every test here is really the
same test asked in different ways — when the stamp is anything other
than a stamp this deploy wrote, does the reader say "I don't know"
instead of guessing?

Absent, unreadable, truncated, corrupt, key-less, revision-less: all
of them collapse to ``None``. The only path that returns a value is a
file with a non-empty ``revision``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_invite_bot.utils import build_info as mod
from telegram_invite_bot.utils.build_info import (
    BUILD_INFO_FILENAME,
    BuildInfo,
    default_root,
    read_build_info,
)


def _write(root: Path, body: str) -> None:
    (root / BUILD_INFO_FILENAME).write_text(body, encoding="utf-8")


def test_full_stamp_round_trips(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "revision=1ac54dc\n"
        "committed_at=2026-08-20 05:10:00 +0300\n"
        "deployed_at=2026-08-20 05:43:11 +0300\n"
        "deployed_by=owner@Mac\n",
    )
    assert read_build_info(tmp_path) == BuildInfo(
        revision="1ac54dc",
        committed_at="2026-08-20 05:10:00 +0300",
        deployed_at="2026-08-20 05:43:11 +0300",
        deployed_by="owner@Mac",
    )


def test_missing_file_is_unknown(tmp_path: Path) -> None:
    """No stamp at all — the normal state of a box deployed by hand
    before #170, and of any checkout that was never deployed."""
    assert read_build_info(tmp_path) is None


def test_directory_in_place_of_file_is_unknown(tmp_path: Path) -> None:
    """``read_bytes`` on a directory raises ``IsADirectoryError``, an
    ``OSError`` — the same silent branch as a permission error, which
    is the realistic one on a root-owned app dir."""
    (tmp_path / BUILD_INFO_FILENAME).mkdir()
    assert read_build_info(tmp_path) is None


def test_revision_absent_is_unknown(tmp_path: Path) -> None:
    """Timestamps without a revision answer the wrong question. The
    operator wants to know *which code*, not when someone last ran
    something."""
    _write(tmp_path, "deployed_at=2026-08-20\ndeployed_by=someone\n")
    assert read_build_info(tmp_path) is None


def test_empty_revision_is_unknown(tmp_path: Path) -> None:
    """A shell that expanded an unset variable writes ``revision=``.
    Rendering an empty ``<code></code>`` would read as a build with a
    blank name rather than as a failure."""
    _write(tmp_path, "revision=\ndeployed_at=2026-08-20\n")
    assert read_build_info(tmp_path) is None


def test_empty_file_is_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "")
    assert read_build_info(tmp_path) is None


def test_lines_without_a_separator_are_skipped(tmp_path: Path) -> None:
    """Someone will eventually put a comment or a banner in here. It
    must not take the revision with it."""
    _write(tmp_path, "# written by scripts/deploy.sh\n\nrevision=abc1234\ngarbage\n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert info.revision == "abc1234"


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """Only the four known keys are read. A future deploy script that
    adds a field cannot make an older bot crash, and a hostile file
    cannot smuggle an attribute in."""
    _write(tmp_path, "revision=abc1234\nbranch=main\ncommand=rm -rf /\n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert info == BuildInfo(
        revision="abc1234", committed_at=None, deployed_at=None, deployed_by=None
    )


def test_keys_are_case_insensitive_and_trimmed(tmp_path: Path) -> None:
    _write(tmp_path, "  REVISION  =  abc1234  \n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert info.revision == "abc1234"


def test_value_containing_equals_survives(tmp_path: Path) -> None:
    """``partition`` splits on the FIRST ``=`` — a timezone or a URL
    in a value must not be chopped."""
    _write(tmp_path, "deployed_by=ci?job=42\nrevision=abc1234\n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert info.deployed_by == "ci?job=42"


def test_long_value_is_truncated(tmp_path: Path) -> None:
    """The card lives inside Telegram's 4096-char message limit and
    shares it with the rest of the status card. A stamp written by a
    runaway shell must cost a bounded number of characters, not the
    whole message."""
    _write(tmp_path, "revision=" + "a" * 500 + "\n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert len(info.revision) == 120
    assert info.revision.endswith("…")


def test_oversized_file_is_read_only_up_to_the_cap(tmp_path: Path) -> None:
    """A stamp is four short lines. Anything past a few KB is not a
    stamp, and the reader must not page a large file into memory on
    every ``/admin_status``."""
    padding = "x" * 8192
    _write(tmp_path, f"{padding}\nrevision=abc1234\n")
    assert read_build_info(tmp_path) is None


def test_undecodable_bytes_do_not_raise(tmp_path: Path) -> None:
    """``errors="replace"`` — a half-written file caught mid-rsync is
    a normal race, not an exception in a request path."""
    (tmp_path / BUILD_INFO_FILENAME).write_bytes(b"revision=abc1234\ndeployed_by=\xff\xfe\n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert info.revision == "abc1234"


def test_default_root_is_the_directory_holding_src() -> None:
    """The stamp sits next to ``.env``, NOT under ``src/`` — the
    deploy rsyncs ``src/`` with ``--delete``, so a stamp inside it
    would be erased by the next deploy right after being written."""
    root = default_root()
    assert (root / "src" / "telegram_invite_bot").is_dir()


def test_default_root_is_used_when_no_root_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "default_root", lambda: tmp_path)
    _write(tmp_path, "revision=deadbee\n")
    info = read_build_info()
    assert info is not None
    assert info.revision == "deadbee"


def test_only_the_first_4kb_is_parsed(tmp_path: Path) -> None:
    """The read stops at ``_MAX_BYTES`` (#510).

    A second ``revision=`` line placed past the ceiling would win if the
    whole file were parsed — later keys overwrite earlier ones — so the
    first revision surviving is proof the tail was never seen. The file
    is opened and read with a bound rather than read whole and sliced,
    which is what ``_MAX_BYTES``' own comment promises.
    """
    tail = "x" * 8192
    _write(tmp_path, f"revision=aaaaaaa\n{tail}\nrevision=bbbbbbb\n")
    info = read_build_info(tmp_path)
    assert info is not None
    assert info.revision == "aaaaaaa"
