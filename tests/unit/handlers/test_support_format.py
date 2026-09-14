"""Unit tests for ``_format_admin_notification`` truncation contract.

Every existing e2e support test sends short feedback messages, so the
ellipsis branch at ``len(text) > _ADMIN_PREVIEW_MAX`` is uncovered.
The branch matters because Telegram caps message size at 4096 chars,
admin DMs include the user's text verbatim, and a 20kB rant from a
single chatty user would otherwise reject the entire DM — admin
notification swallowed silently, support workflow degraded.

We unit-test the helper directly: the 1500-char limit is internal,
and driving it through a real ``/feedback`` would make the e2e log
unreadable.
"""

from __future__ import annotations

from telegram_invite_bot.handlers.support import (
    _ADMIN_PREVIEW_MAX,
    _format_admin_notification,
)


def _build(text: str) -> str:
    return _format_admin_notification(uid=42, first_name="Алиса", username="alice", text=text)


def test_short_text_renders_without_ellipsis() -> None:
    """At-or-below the cap → no truncation marker. Pins the boundary
    so a future off-by-one (e.g. ``>=`` instead of ``>``) would fail
    here rather than silently appending "…" to every message.
    """
    body = _build("hello")
    assert "hello" in body
    assert "…" not in body


def test_exactly_at_cap_renders_without_ellipsis() -> None:
    """Boundary: ``len(text) == _ADMIN_PREVIEW_MAX`` is NOT truncated.
    Locks the inclusive cap so admins see the full text when a user's
    message lands exactly on the limit.
    """
    body = _build("x" * _ADMIN_PREVIEW_MAX)
    assert "…" not in body
    # Full payload is present.
    assert "x" * _ADMIN_PREVIEW_MAX in body


def test_over_cap_appends_ellipsis_and_truncates() -> None:
    """A message longer than 1500 chars must be truncated to exactly
    the cap and have an ellipsis appended. The branch surfaces visibly
    in the admin DM — an admin replying to a truncated complaint
    knows to ask for the rest.
    """
    text = "y" * (_ADMIN_PREVIEW_MAX + 50)
    body = _build(text)
    assert body.endswith("…")
    # The full original text is NOT in the body — only the 1500-char prefix.
    assert text not in body
    # And the prefix IS in the body.
    assert "y" * _ADMIN_PREVIEW_MAX in body
