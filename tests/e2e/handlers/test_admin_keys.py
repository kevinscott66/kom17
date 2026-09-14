"""End-to-end ``/admin_keys``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs /
  EACCES).
* Parser: 9-field key row; short rows dropped defensively.
* Flag discriminator: ``R`` / ``D`` / ``i`` → revoked=True.
* Timeout discriminator: ``expd`` → expired=True; ``perm`` /
  ``3w`` → expired=False.
* ⚠ fires iff unhealthy_count > 0; must NOT fire on healthy
  keyring (cry-wolf pin).
* Empty keyring is normal (EACCES-equivalent) — must NOT warn.
* Group invocation → router-level private filter rejects.
* Every /proc/keys-derived column is HTML-escaped (#1596): the
  card goes out with parse_mode=HTML and the description column
  is free text chosen by whoever created the key.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.keys import (
    _capture,
    _Key,
    _KeysSnapshot,
    _parse_keys,
    _render,
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
        make_message_update("/admin_keys", user_id=42, chat_type="private"),
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
        make_message_update("/admin_keys", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Kernel keyring" in text


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
            "/admin_keys",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "0a2b3c4d I--Q---     1 perm     3f010000     0     0 user      docker_hub: 21\n"
    "1b3c4d5e I------    25 3w       3f010000     0     0 keyring   _ses: 1\n"
    "2c4d5e6f I------     1 perm     3f010000     0     0 dns_resolver dns:foo.example: 17\n"
)


def test_parse_canonical() -> None:
    keys = _parse_keys(_SAMPLE)
    assert len(keys) == 3
    by_id = {k.id: k for k in keys}
    assert by_id["0a2b3c4d"].type == "user"
    assert by_id["0a2b3c4d"].description == "docker_hub: 21"
    assert by_id["1b3c4d5e"].type == "keyring"
    assert by_id["2c4d5e6f"].type == "dns_resolver"


def test_parse_short_row_dropped() -> None:
    """A row missing fields (corrupt / truncated read) drops.
    Pinned because synthesising a key with empty type would
    break the by-type aggregation and the operator's mental
    model."""
    text = "0a2b3c4d I--Q--- 1 perm 0\n0a2b3c4d I--Q--- 1 perm 3f010000 0 0 user ok: 1\n"
    keys = _parse_keys(text)
    assert [k.description for k in keys] == ["ok: 1"]


def test_parse_revoked_flag() -> None:
    """R in flags → revoked=True. Pinned because this is one
    half of the ⚠ predicate."""
    text = "0a2b3c4d IR-----     1 perm     3f010000     0     0 user      gone: 0\n"
    keys = _parse_keys(text)
    assert keys[0].revoked is True
    assert keys[0].expired is False


def test_parse_expired_timeout() -> None:
    """timeout='expd' → expired=True. The other half of ⚠."""
    text = "0a2b3c4d I------     1 expd     3f010000     0     0 user      stale: 0\n"
    keys = _parse_keys(text)
    assert keys[0].expired is True
    assert keys[0].revoked is False


def test_parse_empty() -> None:
    assert _parse_keys("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.keys == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "keys"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.keys) == 3
    assert snap.unhealthy_count == 0


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _KeysSnapshot(keys=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    """An empty keyring is NORMAL on a minimal daemon — must NOT
    warn. Pinned because conflating 'empty keyring' with 'broken
    keyring' would burn the operator on every non-LUKS host."""
    snap = _KeysSnapshot(keys=(), available=True)
    text = _render(snap)
    assert "Empty keyring" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy_keyring(tmp_path: Path) -> None:
    """Cry-wolf pin: ⚠ MUST NOT appear when every key is
    instantiated and unexpired. Pinned because the entire
    predicate is revoked-or-expired."""
    p = tmp_path / "keys"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warning_on_revoked(tmp_path: Path) -> None:
    """⚠ fires when at least one key has R flag."""
    p = tmp_path / "keys"
    p.write_text(
        _SAMPLE + "ffffffff IR-----     1 perm     3f010000     0     0 user      bad: 0\n"
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "revoked" in text
    assert "ffffffff" in text


def test_render_warning_on_expired(tmp_path: Path) -> None:
    """⚠ fires when at least one key has timeout=expd."""
    p = tmp_path / "keys"
    p.write_text(
        _SAMPLE + "eeeeeeee I------     1 expd     3f010000     0     0 user      old: 0\n"
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "expired" in text
    assert "eeeeeeee" in text


def test_key_fields() -> None:
    k = _Key(
        id="abc",
        flags="IR-----",
        timeout="perm",
        type="user",
        description="x: 1",
    )
    assert k.revoked is True
    assert k.expired is False


def test_render_escapes_proc_derived_columns(tmp_path: Path) -> None:
    """#1596: the card is HTML, /proc/keys is not.

    The description column is a key type's describe() output, and
    for a ``user`` key that is whatever string the creating process
    passed in — so it can contain ``<`` and ``&``. Unescaped, a
    stray ``<`` breaks Telegram's HTML parser and the operator gets
    an API error instead of the card they opened during an
    incident. The row must show the text, not interpret it."""
    p = tmp_path / "keys"
    p.write_text(
        _SAMPLE
        + "ffffffff IR-----     1 perm     3f010000     0     0 user      "
        + "<b>a&b</b>: 0\n"
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "&lt;b&gt;a&amp;b&lt;/b&gt;" in text
    # The raw form must not survive anywhere on the card.
    assert "<b>a&b</b>" not in text


def test_render_escapes_the_narrow_columns_too() -> None:
    """The id / type / flags / timeout columns are escaped with the
    description rather than left as a per-column judgement call
    (#1596). They have never carried markup — they are
    whitespace-delimited tokens — but they come off the same line
    from the same unprivileged producer."""
    snap = _KeysSnapshot(
        keys=(
            _Key(
                id="a<b",
                flags="IR&----",
                timeout="p<m",
                type="us&r",
                description="x: 1",
            ),
        ),
        available=True,
    )
    text = _render(snap)
    assert "a&lt;b" in text
    assert "IR&amp;----" in text
    assert "p&lt;m" in text
    # Both the by-type summary and the unhealthy row carry the type.
    assert text.count("us&amp;r") == 2
    assert "us&r</code>" not in text
