"""#1958: a stored welcome template is DATA, not a format string.

``render_welcome_template`` used to hand the admin-typed template to
``str.format_map``. Escaping the template first (L-57) neutralises
markup, but it does nothing to the *format mini-language*, which
``format_map`` still interprets:

* ``{user:99999999}`` is a width — the renderer pads the joiner's name
  out to a hundred million characters. ``html.escape`` leaves the digits
  untouched (no ``<``, ``>``, ``&``), and the 1000-char template cap
  buys ~66 repeats, i.e. multi-gigabyte allocation. The resulting
  ``MemoryError`` is not in the handler's ``except (ValueError,
  IndexError)``, so it takes the worker down — and the template is
  stored, so it fires again on every single join.
* ``{user.__class__}`` is an attribute access whose result is
  substituted RAW, after the escaping pass — the one seam L-57 exists to
  close.
* ``{user.foo}`` raises ``AttributeError``, also outside that except.

Anyone can be the admin of a group they created and added the bot to,
so ``_require_admin`` is not a trust boundary here.

The fix renders the template as plain text with exactly two recognised
tokens, ``{user}`` and ``{chat}``, in one left-to-right pass. Everything
else — including every spec, conversion and attribute form above —
survives as literal text, which is what an admin who typed it sees.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.group_events import render_welcome_template


def test_a_width_spec_cannot_blow_the_output_up() -> None:
    """The bomb: on HEAD this allocated 20 MB from 15 characters."""
    out = render_welcome_template("{user:20000000}", user="Bob", chat="C")

    assert out == "{user:20000000}"


def test_the_length_cap_does_not_bound_the_output() -> None:
    """Repeats are what turn the cap into gigabytes, so pin them too."""
    template = "{user:200000}" * 20  # 260 chars, well under the 1000 cap

    out = render_welcome_template(template, user="Bob", chat="C")

    assert len(out) == len(template)


def test_an_attribute_access_cannot_smuggle_raw_markup() -> None:
    """``{user.__class__}`` rendered ``<class 'str'>`` past the escaper."""
    out = render_welcome_template("{user.__class__}", user="Bob", chat="C")

    assert "<" not in out
    assert out == "{user.__class__}"


def test_a_missing_attribute_no_longer_raises() -> None:
    """``AttributeError`` was outside the handler's except clause."""
    out = render_welcome_template("{user.foo}", user="Bob", chat="C")

    assert out == "{user.foo}"


@pytest.mark.parametrize(
    "template",
    ["{user!r}", "{user:c}", "{user:>10}", "{0}", "{}", "{user", "}{"],
)
def test_every_other_format_form_is_literal_text(template: str) -> None:
    """Nothing but the two bare tokens is interpreted, ever."""
    out = render_welcome_template(template, user="Bob", chat="C")

    assert "Bob" not in out


def test_the_two_real_placeholders_still_substitute() -> None:
    """The half that must not change."""
    out = render_welcome_template("Hi {user}, welcome to {chat}!", user="Bob", chat="Cats")

    assert out == "Hi Bob, welcome to Cats!"


def test_substitution_is_still_a_single_pass() -> None:
    """A joiner named ``{chat}`` must not leak the chat title."""
    out = render_welcome_template("Hi {user}", user="{chat}", chat="SECRET")

    assert out == "Hi {chat}"


def test_both_halves_are_still_escaped() -> None:
    """L-57 itself: template markup and value markup both die."""
    out = render_welcome_template("<b>{user}</b>", user="<i>x</i>", chat="C")

    assert "<b>" not in out
    assert "<i>" not in out
    assert out == "&lt;b&gt;&lt;i&gt;x&lt;/i&gt;&lt;/b&gt;"
