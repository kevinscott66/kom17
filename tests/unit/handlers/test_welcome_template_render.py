"""Unit tests for the L-57 welcome-template renderer (security-critical).

The renderer must never emit live HTML from either the stored template
or the substituted ``{user}`` / ``{chat}`` values — the card is sent with
parse_mode=HTML, so an unescaped ``<`` is an injection seam.
"""

from __future__ import annotations

from telegram_invite_bot.handlers.group_events import render_welcome_template


def test_placeholders_substituted() -> None:
    out = render_welcome_template("Hi {user}, welcome to {chat}!", user="Bob", chat="Cats")
    assert out == "Hi Bob, welcome to Cats!"


def test_template_html_is_escaped() -> None:
    # An admin trying to store live markup gets it neutralised.
    out = render_welcome_template("<b>{user}</b>", user="Bob", chat="C")
    assert "<b>" not in out
    assert out == "&lt;b&gt;Bob&lt;/b&gt;"


def test_user_value_html_is_escaped() -> None:
    # A joiner with a malicious display name cannot inject markup.
    out = render_welcome_template("Hi {user}", user="<a href='x'>x</a>", chat="C")
    assert "<a" not in out
    assert "&lt;a href=&#x27;x&#x27;&gt;x&lt;/a&gt;" in out


def test_chat_value_html_is_escaped() -> None:
    out = render_welcome_template("Welcome to {chat}", user="U", chat="<i>evil</i>")
    assert "<i>" not in out
    assert "&lt;i&gt;evil&lt;/i&gt;" in out


def test_no_placeholder_is_passthrough_escaped() -> None:
    out = render_welcome_template("plain & <text>", user="U", chat="C")
    assert out == "plain &amp; &lt;text&gt;"


def test_value_cannot_smuggle_placeholder() -> None:
    # A user named literally "{chat}" must NOT cause a second substitution
    # pass that leaks the chat title — substitution is single-pass and the
    # escaped value's braces are inert.
    out = render_welcome_template("Hi {user}", user="{chat}", chat="SECRET")
    assert "SECRET" not in out
    assert out == "Hi {chat}"
