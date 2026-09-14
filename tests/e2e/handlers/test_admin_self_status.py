"""End-to-end ``/admin_self_status``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux self/status) OR unavailable note.
* Parser filters to ``_INTERESTING`` keyset; unknown keys
  silently dropped (forward-compat).
* TracerPid non-zero → is_traced=True → ⚠.
* CoreDumping == 1 → core_dumping=True → ⚠.
* Both ⚠ predicates must NOT fire on canonical-healthy sample
  (cry-wolf guard).
* Seccomp decode: 0=disabled, 1=strict, 2=filter, other=unknown.
* Defensive int parse — non-numeric TracerPid value treated as
  zero, no spurious ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.self_status import (
    _capture,
    _format_seccomp,
    _parse_self_status,
    _render,
    _SelfStatusSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_self_status", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_self_status", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Process posture" in text


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
        make_message_update(
            "/admin_self_status",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "Name:\tpython3\n"
    "State:\tR (running)\n"
    "Tgid:\t1234\n"
    "Pid:\t1234\n"
    "PPid:\t1\n"
    "TracerPid:\t0\n"
    "Uid:\t1000\t1000\t1000\t1000\n"
    "Gid:\t1000\t1000\t1000\t1000\n"
    "FDSize:\t256\n"
    "Threads:\t4\n"
    "NSpid:\t1234\n"
    "Seccomp:\t2\n"
    "Seccomp_filters:\t1\n"
    "NoNewPrivs:\t1\n"
    "Speculation_Store_Bypass:\tthread mitigated\n"
    "CoreDumping:\t0\n"
    "Cpus_allowed_list:\t0-7\n"
    "future_field_2030:\tmagic\n"
)


def test_parse_canonical() -> None:
    fields = _parse_self_status(_SAMPLE)
    assert fields["Name"] == "python3"
    assert fields["TracerPid"] == "0"
    assert fields["Seccomp"] == "2"
    assert fields["CoreDumping"] == "0"
    assert fields["Speculation_Store_Bypass"] == "thread mitigated"


def test_parse_unknown_keys_ignored() -> None:
    """Forward-compat: kernel additions we don't model must
    drop silently, not crash render."""
    fields = _parse_self_status(_SAMPLE)
    assert "future_field_2030" not in fields
    # Tgid isn't in _INTERESTING — confirm it's filtered.
    assert "Tgid" not in fields


def test_parse_line_without_colon_dropped() -> None:
    """Defensive: a corrupt line missing the ':' separator
    drops rather than crashing."""
    text = "no colon here\nSeccomp:\t1\n"
    fields = _parse_self_status(text)
    assert fields == {"Seccomp": "1"}


def test_parse_empty() -> None:
    assert _parse_self_status("") == {}


# --- snapshot booleans ----------------------------------------------------


def test_snapshot_traced_when_tracer_nonzero() -> None:
    snap = _SelfStatusSnapshot(
        fields={"TracerPid": "999", "CoreDumping": "0"},
        available=True,
    )
    assert snap.is_traced is True
    assert snap.core_dumping is False


def test_snapshot_core_dumping_when_one() -> None:
    snap = _SelfStatusSnapshot(
        fields={"TracerPid": "0", "CoreDumping": "1"},
        available=True,
    )
    assert snap.is_traced is False
    assert snap.core_dumping is True


def test_snapshot_defensive_non_numeric_tracer() -> None:
    """A future kernel emitting a non-numeric TracerPid value
    must NOT trigger a spurious ⚠. We treat unparseable values
    as zero — the alternative (crash render) is worse, and a
    false ⚠ on every host would burn the operator."""
    snap = _SelfStatusSnapshot(
        fields={"TracerPid": "garbage", "CoreDumping": "0"},
        available=True,
    )
    assert snap.is_traced is False


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.fields == {}


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "status"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert snap.tracer_pid == 0
    assert snap.core_dumping is False


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _SelfStatusSnapshot(fields={}, available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _SelfStatusSnapshot(fields={}, available=True)
    text = _render(snap)
    assert "no recognised keys" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy(tmp_path: Path) -> None:
    """Cry-wolf pin: ⚠ MUST NOT appear when TracerPid=0 AND
    CoreDumping=0. Pinned because both predicates together
    cover the warning surface — a false positive on a healthy
    host would burn the operator every invocation."""
    p = tmp_path / "status"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_traced(tmp_path: Path) -> None:
    p = tmp_path / "status"
    p.write_text(_SAMPLE.replace("TracerPid:\t0", "TracerPid:\t9999"))
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "traced" in text


def test_render_warns_on_coredumping(tmp_path: Path) -> None:
    p = tmp_path / "status"
    p.write_text(_SAMPLE.replace("CoreDumping:\t0", "CoreDumping:\t1"))
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "CoreDumping" in text


def test_render_both_warnings(tmp_path: Path) -> None:
    """When both predicates fire we surface a combined-cause
    narrative (debugger post-mortem) rather than two separate
    paragraphs — pinned distinct from the single-predicate
    branches to keep the rendered explanation actionable."""
    p = tmp_path / "status"
    body = _SAMPLE.replace("TracerPid:\t0", "TracerPid:\t9999").replace(
        "CoreDumping:\t0", "CoreDumping:\t1"
    )
    p.write_text(body)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "post-mortem" in text


# --- helpers ---------------------------------------------------------------


def test_format_seccomp_decodes() -> None:
    assert _format_seccomp("0") == "0 (disabled)"
    assert _format_seccomp("1") == "1 (strict)"
    assert _format_seccomp("2") == "2 (filter)"
    assert _format_seccomp("99") == "99 (unknown)"
