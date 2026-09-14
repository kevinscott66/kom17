"""#120 — the /groupadmin staff surfaces are bounded, and say so.

The staff roster is assembled from two halves. The database half was
always capped (``_STAFF_PROBE_LIMIT``); the ``get_chat_administrators``
half was not, and Telegram lets a supergroup carry dozens of admins,
each with up to 64 characters of user-controlled ``first_name``. The
card therefore had no ceiling at all, and a card over 4096 UTF-16 units
is answered with a 400 — from the admin's seat, a button that does
nothing. The demote grid grew a button per removable person in exactly
the same unbounded way.

These tests pin the three cut-offs that close it: rows, buttons, and
the per-name clip — and, in every case, that the cut-off is disclosed
in the copy rather than applied silently.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from telegram_invite_bot.handlers.groupadmin import (
    _STAFF_DROP_MAX,
    _STAFF_NAME_MAX,
    _STAFF_ROWS_MAX,
    StaffMember,
    StaffRoster,
    _render_staff,
    _render_staff_page,
    _staff_label,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.groupadmin import PAGE_STAFF_DROP
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

_CHAT = -100123
_USER = 555


def _member(user_id: int, *, name: str = "N", rank: int = 2) -> StaffMember:
    return StaffMember(user_id=user_id, name=name, rank=rank, tg_admin=False)


def _crowd(count: int, *, name_len: int = 64) -> tuple[StaffMember, ...]:
    # 64 is Telegram's own first_name ceiling — the worst case a real
    # chat can hand us, not an invented one.
    return tuple(_member(1000 + i, name="Ы" * name_len) for i in range(count))


# ── the per-name clip ────────────────────────────────────────────────────────


def test_staff_label_leaves_a_normal_name_alone() -> None:
    assert _staff_label("Аня") == "Аня"


def test_staff_label_clips_and_marks_the_clip() -> None:
    label = _staff_label("Я" * 100)
    assert len(label) == _STAFF_NAME_MAX + 1  # + the ellipsis
    assert label.endswith("…")


def test_staff_label_counts_utf16_units_not_code_points() -> None:
    # An astral emoji is one code point but TWO UTF-16 units, and UTF-16
    # is what Telegram counts against the 4096 ceiling. Measuring with
    # len() would let a name twice the intended budget through.
    label = _staff_label("🙂" * _STAFF_NAME_MAX)
    assert len(label.rstrip("…")) <= _STAFF_NAME_MAX // 2


def test_staff_label_never_splits_a_surrogate_pair() -> None:
    # Half an emoji is not a character. #1981: the body used to be the
    # bare call ``_staff_label("🙂" * 40).encode("utf-16-le")`` with no
    # assertion, and it could not have failed — a Python ``str`` cannot
    # hold half a pair, so the encode was testing the language. What is
    # ours is the clip: it must land on a code-point boundary AND stay
    # inside the UTF-16 budget the neighbours above measure.
    label = _staff_label("🙂" * 40)

    assert label.rstrip("…") == "🙂" * len(label.rstrip("…"))
    assert len(label.rstrip("…").encode("utf-16-le")) // 2 <= _STAFF_NAME_MAX


# ── the roster rows ──────────────────────────────────────────────────────────


def test_render_staff_caps_rows_and_fits_telegram() -> None:
    crowd = _crowd(120)
    text = _render_staff(
        StaffRoster(members=crowd, truncated=False),
        "ru",
        title="Ж" * 64,
        can_manage=True,
    )
    rendered = sum(1 for m in crowd if f"<code>{m.user_id}</code>" in text)
    assert rendered == _STAFF_ROWS_MAX
    # The whole point: 120 admins with maximum-length names used to
    # produce a card Telegram answers with a 400.
    assert parsed_length(text) <= TELEGRAM_TEXT_LIMIT


def test_render_staff_counts_everyone_but_shows_a_page() -> None:
    # The count line is about the group; the rows are about the screen.
    # Reporting the rendered number as the total would hide exactly the
    # fact the truncation line exists to disclose.
    text = _render_staff(
        StaffRoster(members=_crowd(37), truncated=False),
        "ru",
        title="g",
        can_manage=True,
    )
    assert t("h_ga_staff_count", "ru", count=37) in text
    assert t("h_ga_staff_truncated", "ru", count=_STAFF_ROWS_MAX) in text


def test_render_staff_says_nothing_when_nothing_was_cut() -> None:
    text = _render_staff(
        StaffRoster(members=_crowd(3), truncated=False),
        "ru",
        title="g",
        can_manage=True,
    )
    assert t("h_ga_staff_truncated", "ru", count=3) not in text
    assert "…показаны первые" not in text


def test_render_staff_still_escapes_a_clipped_name() -> None:
    # Clipping happens before escaping; a name that is pure markup must
    # not arrive at Telegram as markup just because it was long.
    text = _render_staff(
        StaffRoster(members=(_member(1, name="<b>" * 40),), truncated=False),
        "ru",
        title="g",
        can_manage=True,
    )
    assert "<b><b>" not in text
    assert "&lt;b&gt;" in text


# ── the demote grid ──────────────────────────────────────────────────────────


async def _drop_page(
    monkeypatch: pytest.MonkeyPatch, people: tuple[StaffMember, ...]
) -> tuple[str, Any]:
    import telegram_invite_bot.handlers.groupadmin as mod

    async def _authority(*args: Any, **kwargs: Any) -> tuple[bool, int]:  # noqa: ARG001
        return True, 5

    async def _fetch(*args: Any, **kwargs: Any) -> StaffRoster:  # noqa: ARG001
        return StaffRoster(members=people, truncated=False)

    monkeypatch.setattr(mod, "_staff_authority", _authority)
    monkeypatch.setattr(mod, "_fetch_staff", _fetch)
    monkeypatch.setattr(mod, "_droppable", lambda *a, **k: list(people))  # noqa: ARG005
    return await _render_staff_page(
        PAGE_STAFF_DROP,
        cast("Any", None),
        _CHAT,
        "ru",
        title="g",
        bot=cast("Any", None),
        settings=cast("Any", None),
        actor_id=_USER,
    )


@pytest.mark.asyncio
async def test_drop_grid_caps_buttons_and_discloses_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, markup = await _drop_page(monkeypatch, _crowd(90))
    demote_buttons = sum(
        1
        for row in markup.inline_keyboard
        for btn in row
        if (btn.callback_data or "").startswith("gadmsd")
    )
    assert demote_buttons == _STAFF_DROP_MAX
    assert t("h_ga_staff_drop_more", "ru", count=90 - _STAFF_DROP_MAX) in text


@pytest.mark.asyncio
async def test_drop_grid_says_nothing_when_everyone_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, _ = await _drop_page(monkeypatch, _crowd(4))
    assert "…и ещё" not in text
    assert t("h_ga_staff_drop_prompt", "ru") in text


@pytest.mark.asyncio
async def test_drop_grid_labels_are_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, markup = await _drop_page(monkeypatch, _crowd(3))
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("…" in label for label in labels)
    assert all(len(label) < 64 for label in labels)
