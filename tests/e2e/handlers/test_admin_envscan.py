"""End-to-end ``/admin_envscan``.

Pins:

* Non-developer → silent drop.
* Card renders with credential + operational sections.
* Credential value is NEVER rendered in full — only length +
  first-2/last-2 preview, and only when the value is ≥ 8 chars.
* Short (< 8 char) values get length but NO preview (the preview
  IS the secret).
* Operational-prefix keys (TG_, BOT_, PYTHON, …) are surfaced by
  NAME only — never with their values.
* Pattern match is on KEY names; a non-matching key with a
  credential-shaped VALUE is NOT flagged (documents the limit).
* Render truncates at ``_MAX_CREDENTIAL_ROWS`` / ``_MAX_OPERATIONAL_ROWS``
  with "… and N more" footer so a CI host with 50+ AUTH_* vars
  doesn't blow the 4096-char card budget.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.envscan import (
    _MAX_CREDENTIAL_ROWS,
    _MIN_PREVIEWABLE_LEN,
    _capture,
    _is_credential_key,
    _is_operational_key,
    _mask_value,
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
        make_message_update("/admin_envscan", user_id=42, chat_type="private"),
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
        make_message_update("/admin_envscan", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Environment scan" in text
    assert "credential-shaped" in text
    assert "operational prefixes" in text


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
            "/admin_envscan",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_credential_pattern_matches() -> None:
    """The patterns must catch the well-known credential shapes.
    Adding a missed pattern is the kind of silent regression the
    pattern table is meant to prevent."""
    for name in (
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DB_PASSWORD",
        "OPENAI_API_KEY",
        "SENTRY_DSN",
        "BOT_TOKEN",
        "MY_AUTH_HEADER",
    ):
        assert _is_credential_key(name), name


def test_credential_pattern_does_not_match_innocuous() -> None:
    """Names that ARE NOT credentials must not be flagged — false
    positives push real credentials off the bottom of the card."""
    for name in ("PATH", "HOME", "USER", "TERM", "LANG", "PYTHONHASHSEED"):
        assert not _is_credential_key(name), name


def test_operational_pattern_matches_known_prefixes() -> None:
    """Operational keys must surface for the well-known prefixes
    the systemd unit + .env use."""
    for name in (
        "TG_FOO",
        "BOT_TOKEN",  # also credential
        "DATABASE_URL",
        "WEBHOOK_SECRET_TOKEN",
        "DEVELOPER_ID_1",
        "PYTHONDEVMODE",
        "LC_ALL",
        "LANG",
    ):
        assert _is_operational_key(name), name


def test_mask_value_short_suppresses_preview() -> None:
    """Sub-threshold value: length yes, preview NO. A 7-char token
    previewed leaves only 3 hidden chars — the preview IS the
    secret. Documented as the suppression rule."""
    length, preview = _mask_value("abcdefg")  # 7 chars
    assert length == 7
    assert preview == ""


def test_mask_value_long_shows_first_last_only() -> None:
    """≥ 8-char value: first-2 + last-2 preview with ellipsis. The
    middle bytes are the entropy and must never appear."""
    length, preview = _mask_value("supersecret12345")
    assert length == 16
    assert preview == "su…45"
    assert "persec" not in preview


def test_mask_value_exactly_threshold() -> None:
    """At exactly ``_MIN_PREVIEWABLE_LEN`` chars the preview kicks
    in — boundary pin so a future tweak of the constant doesn't
    silently change the cut-off."""
    val = "a" * _MIN_PREVIEWABLE_LEN
    length, preview = _mask_value(val)
    assert length == _MIN_PREVIEWABLE_LEN
    assert preview != ""


def test_capture_partitions_correctly() -> None:
    """End-to-end on a synthetic env: credential keys go to one
    bucket, operational to the other, plain keys are ignored,
    overlap (BOT_TOKEN matches both) goes to credentials."""
    env = {
        "GITHUB_TOKEN": "ghp_abcdefghij1234567890",
        "BOT_TOKEN": "1:abcdef",
        "DATABASE_URL": "sqlite:///x.db",
        "PYTHONHASHSEED": "1",  # operational but not credential
        "HOME": "/home/x",  # neither
        "TG_FOO": "bar",  # operational only
    }
    snap = _capture(env=env)
    # GITHUB_TOKEN is credential; BOT_TOKEN matches both but
    # credential takes precedence per the capture branching.
    assert "GITHUB_TOKEN" in snap.credential_keys
    assert "BOT_TOKEN" in snap.credential_keys
    assert "BOT_TOKEN" not in snap.operational_keys
    assert "PYTHONHASHSEED" in snap.operational_keys
    assert "TG_FOO" in snap.operational_keys
    assert "HOME" not in snap.credential_keys
    assert "HOME" not in snap.operational_keys


def test_capture_value_never_in_render() -> None:
    """The single load-bearing security pin: the actual credential
    string must never appear in the rendered card. This is the
    one assertion that, if it breaks, means we've actually leaked
    a secret to the chat."""
    secret = "ghp_VERYLONGSECRETVALUE1234567890ABCDEF"
    env = {"GITHUB_TOKEN": secret, "HOME": "/home/x"}
    snap = _capture(env=env)
    rendered = _render(snap)
    assert secret not in rendered
    # The middle portion specifically — the masked preview shows
    # only the first 2 + last 2, so the entropic interior must
    # never appear.
    assert "VERYLONGSECRETVALUE" not in rendered


def test_render_truncates_long_credential_list() -> None:
    """A host with 50+ AUTH_* vars (CI runners, multi-tenant
    deploys) would blow the 4096-char card budget. Truncation
    keeps the card readable; "… and N more" tells the operator
    how much was elided so they know to inspect via shell if
    needed."""
    env = {f"AUTH_VAR_{i:03d}": "abcdefghij" for i in range(_MAX_CREDENTIAL_ROWS + 10)}
    snap = _capture(env=env)
    rendered = _render(snap)
    assert "… and 10 more" in rendered


def test_render_empty_environment() -> None:
    """A perfectly clean env (no credentials, no operational
    prefixes) renders both sections as "none" without any error
    or "0 …" weirdness. Boundary pin."""
    snap = _capture(env={"PATH": "/usr/bin", "HOME": "/home/x"})
    rendered = _render(snap)
    assert "credential-shaped:</b> <i>none" in rendered
    assert "operational prefixes:</b> <i>none" in rendered


def test_render_escapes_html_in_names_and_previews() -> None:
    """#191: both halves of a credential row are attacker-shaped.

    The preview is two raw bytes off each end of a real secret and the
    name is whatever the host env happens to carry — a single ``<`` or
    ``&`` in either turns the whole ``parse_mode=HTML`` card into a
    Telegram 400, and the operator sees nothing at all. That is the
    worst possible failure for a diagnostic command: it goes dark
    exactly on the host whose env is odd enough to be worth scanning.
    """
    env = {"AUTH_<b>&_KEY": "<&secretvalue&>", "TG_<i>&_MODE": "x"}
    snap = _capture(env=env)
    rendered = _render(snap)
    # Names — both the credential row and the operational row.
    assert "<b>&_KEY" not in rendered
    assert "AUTH_&lt;b&gt;&amp;_KEY" in rendered
    assert "TG_&lt;i&gt;&amp;_MODE" in rendered
    # Preview — _mask_value keeps the first two and last two bytes, so
    # both ends of this value are escapable.
    assert "&lt;&amp;…&amp;&gt;" in rendered
