"""End-to-end ``/admin_key_users``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note.
* Parser: 5-token format, ``uid:`` colon-suffix, ``a/b`` pair tokens.
* Defensive: missing colon, non-int uid, malformed a/b pair drop.
* Ratio-based ⚠ — not absolute (kernel.keys.maxkeys is tunable).
* Cry-wolf must-not-fire when every uid below 80%.
* ⚠ fires when keys-ratio OR bytes-ratio >= threshold.
* maxkeys=0 / maxbytes=0 don't crash (defensive division).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.key_users import (
    _QUOTA_WARN_RATIO,
    _capture,
    _fmt_pct,
    _KeyUserRow,
    _KeyUsersSnapshot,
    _parse_key_users,
    _parse_pair,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


_SAMPLE = "    0:     7 6/6 4/200 51/20000\n 1000:     2 2/2 2/200 18/20000\n"


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_key_users", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_key_users", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "keyring quota" in text


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
            "/admin_key_users", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def test_parse_canonical() -> None:
    rows = _parse_key_users(_SAMPLE)
    assert len(rows) == 2
    root = rows[0]
    assert root.uid == 0
    assert root.usage == 7
    assert root.nkeys == 6
    assert root.qnkeys == 4
    assert root.maxkeys == 200
    assert root.qnbytes == 51
    assert root.maxbytes == 20000


def test_parse_missing_colon_dropped() -> None:
    rows = _parse_key_users("0 7 6/6 4/200 51/20000\n")
    assert rows == []


def test_parse_non_int_uid_dropped() -> None:
    rows = _parse_key_users("foo: 7 6/6 4/200 51/20000\n")
    assert rows == []


def test_parse_malformed_pair_dropped() -> None:
    rows = _parse_key_users("0: 7 6 4/200 51/20000\n")
    assert rows == []


def test_parse_short_line_dropped() -> None:
    rows = _parse_key_users("0: 7 6/6\n")
    assert rows == []


def test_parse_empty() -> None:
    assert _parse_key_users("") == []


def test_parse_pair_helper() -> None:
    assert _parse_pair("4/200") == (4, 200)
    assert _parse_pair("no_slash") is None
    assert _parse_pair("x/200") is None
    assert _parse_pair("4/y") is None


# --- ratio + warn ---------------------------------------------------------


def test_keys_ratio_basic() -> None:
    r = _KeyUserRow(uid=0, usage=1, nkeys=1, qnkeys=160, maxkeys=200, qnbytes=0, maxbytes=20000)
    assert r.keys_ratio == 0.8
    assert r.over_quota_warn is True


def test_bytes_ratio_basic() -> None:
    r = _KeyUserRow(uid=0, usage=1, nkeys=1, qnkeys=0, maxkeys=200, qnbytes=18000, maxbytes=20000)
    assert r.bytes_ratio == 0.9
    assert r.over_quota_warn is True


def test_below_threshold() -> None:
    """Cry-wolf component: ratio just below threshold must NOT
    trigger ⚠. 79% of 200 = 158 keys — over the absolute count
    of the kernel default but still under the percentage line."""
    r = _KeyUserRow(uid=0, usage=1, nkeys=1, qnkeys=158, maxkeys=200, qnbytes=0, maxbytes=20000)
    assert r.over_quota_warn is False


def test_maxkeys_zero_no_crash() -> None:
    """An exotic kernel emitting maxkeys=0 (quota fully
    disabled, or a misconfig) must NOT crash render with a
    DivisionByZero. We treat ``qnkeys > 0`` with cap=0 as
    'infinitely tight' → ratio 1.0; ``qnkeys == 0`` with cap=0
    as 'nothing used' → ratio 0.0. Either way, no exception."""
    full = _KeyUserRow(uid=0, usage=1, nkeys=1, qnkeys=1, maxkeys=0, qnbytes=0, maxbytes=20000)
    empty = _KeyUserRow(uid=0, usage=1, nkeys=1, qnkeys=0, maxkeys=0, qnbytes=0, maxbytes=20000)
    assert full.keys_ratio == 1.0
    assert empty.keys_ratio == 0.0


def test_threshold_constant_sane() -> None:
    """Guard against an accidental edit that would set the
    threshold to 0 (every uid would warn) or > 1 (no uid would
    ever warn). The 0.8 value matches operator-intuitive
    'approaching limit' across disk/fd/inode cards."""
    assert 0.5 < _QUOTA_WARN_RATIO < 1.0


# --- snapshot --------------------------------------------------------------


def test_snapshot_warning_rows_filter() -> None:
    rows = [
        _KeyUserRow(uid=0, usage=1, nkeys=1, qnkeys=180, maxkeys=200, qnbytes=0, maxbytes=20000),
        _KeyUserRow(uid=1, usage=1, nkeys=1, qnkeys=1, maxkeys=200, qnbytes=0, maxbytes=20000),
    ]
    snap = _KeyUsersSnapshot(rows=rows, available=True)
    assert [r.uid for r in snap.warning_rows] == [0]


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.rows == []


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "key-users"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 2


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _KeyUsersSnapshot(rows=[], available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_no_uids_but_available() -> None:
    snap = _KeyUsersSnapshot(rows=[], available=True)
    text = _render(snap)
    assert "No uids accounted" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy(tmp_path: Path) -> None:
    """Cry-wolf pin: canonical sample has every uid well below
    80% (4/200 = 2%, 51/20000 = 0.25%). ⚠ MUST NOT appear."""
    p = tmp_path / "key-users"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_quota(tmp_path: Path) -> None:
    p = tmp_path / "key-users"
    p.write_text("0: 7 6/6 180/200 51/20000\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "EDQUOT" in text


def test_render_warns_on_bytes_quota(tmp_path: Path) -> None:
    """Either ratio crossing the threshold must fire — pinned
    distinct from the keys-count branch to guard against an
    edit that accidentally checks only one of the two."""
    p = tmp_path / "key-users"
    p.write_text("0: 7 6/6 4/200 18000/20000\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text


# --- helpers ---------------------------------------------------------------


def test_fmt_pct() -> None:
    assert _fmt_pct(0.0) == "0%"
    assert _fmt_pct(0.5) == "50%"
    assert _fmt_pct(1.0) == "100%"
