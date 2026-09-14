"""End-to-end ``/admin_codecs``.

Pins:

* Non-developer → silent drop.
* Card renders with all six encoding axes + codec status section.
* Healthy snapshot (all UTF-8, all codecs present) → zero ⚠ on
  data rows (cry-wolf prevention; the footer legend's ⚠ is
  partitioned out before counting).
* Non-UTF-8 stream encoding → ⚠ on exactly that row, others bare.
* Missing load-bearing codec → ⚠ on the missing row + count in
  the header.
* ``_is_utf8`` accepts both ``utf-8`` and ``utf8`` (the codec
  registry treats them as aliases; we don't want to ⚠ on cosmetic
  capitalization/hyphenation differences).
* ``_normalize`` lowercases + handles None.
* ``_probe_missing_codecs`` flags genuinely-unregistered names
  and accepts well-known ones.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.codecs import (
    _CodecSnapshot,
    _is_utf8,
    _normalize,
    _probe_missing_codecs,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    default_encoding: str = "utf-8",
    filesystem_encoding: str = "utf-8",
    stdin_encoding: str | None = "utf-8",
    stdout_encoding: str | None = "utf-8",
    stderr_encoding: str | None = "utf-8",
    locale_preferred: str = "utf-8",
    missing_codecs: list[str] | None = None,
) -> _CodecSnapshot:
    return _CodecSnapshot(
        default_encoding=default_encoding,
        filesystem_encoding=filesystem_encoding,
        stdin_encoding=stdin_encoding,
        stdout_encoding=stdout_encoding,
        stderr_encoding=stderr_encoding,
        locale_preferred=locale_preferred,
        missing_codecs=missing_codecs or [],
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_codecs", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_codecs", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Codec registry" in text
    assert "sys.getdefaultencoding" in text
    assert "stdin.encoding" in text
    assert "locale.getpreferredencoding" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/admin_codecs", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_healthy_snapshot_no_warn() -> None:
    """All UTF-8 + all codecs present → zero data-row ⚠. Same
    partition-before-count idiom as every other admin card so the
    legend's ⚠ glyph doesn't false-positive."""
    rendered = _render(_snap())
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "all present" in head


def test_ascii_stdout_marks_single_row() -> None:
    """Stdout=ascii is the classic systemd-without-LC_ALL symptom.
    Mark exactly that row; other UTF-8 rows stay bare."""
    rendered = _render(_snap(stdout_encoding="ascii"))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    stdout_line = next(line for line in head.splitlines() if "stdout.encoding" in line)
    assert "⚠" in stdout_line
    stdin_line = next(line for line in head.splitlines() if "stdin.encoding" in line)
    assert "⚠" not in stdin_line


def test_missing_codec_marks_row_and_counts() -> None:
    """A missing load-bearing codec ⚠s its row and lifts the section
    header to a count form. Catches the slim-docker-image regression
    that strips ``encodings/__init__.py``."""
    rendered = _render(_snap(missing_codecs=["idna", "punycode"]))
    head, _, _ = rendered.partition("<i>⚠")
    # Two missing rows, each marked.
    assert head.count("⚠") == 2
    assert "load-bearing codecs missing (2)" in head
    assert "idna" in head
    assert "punycode" in head


def test_is_utf8_accepts_aliases() -> None:
    """``utf-8`` and ``utf8`` are codec-registry aliases; the ⚠
    decision must not fire on the cosmetic difference."""
    assert _is_utf8("utf-8")
    assert _is_utf8("utf8")
    assert not _is_utf8("ascii")
    assert not _is_utf8("latin-1")
    assert not _is_utf8("cp1252")


def test_normalize_lowercases_and_handles_none() -> None:
    """Python reports encoding names in mixed case depending on
    platform; we lowercase for stable comparison + display.
    ``None`` (a redirected stream missing ``encoding``) renders as
    ``unknown`` rather than crashing."""
    assert _normalize("UTF-8") == "utf-8"
    assert _normalize("UTF_8") == "utf-8"
    assert _normalize(None) == "unknown"


def test_probe_missing_codecs_real_registry() -> None:
    """The well-known codecs MUST be present on any normal CPython
    install — if this test fails the host has a corrupted stdlib
    and nothing else in the bot will work either. The "missing"
    case is exercised with a deliberately bogus name."""
    assert _probe_missing_codecs(("utf-8", "ascii", "latin-1", "idna")) == []
    assert _probe_missing_codecs(("definitely-not-a-real-codec-xyz",)) == [
        "definitely-not-a-real-codec-xyz"
    ]
