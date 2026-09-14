"""#1952: the /ai refusal copy promised a UTC reset the counter never does.

``AiQuotaRepo._today_iso`` keys ``ai_daily_requests`` on the **host's
local** calendar day, and its docstring names the reason: legacy used
``date.today()``, and keying on the UTC day "would move the reset to
03:00 MSK — contradicting the refusal copy that tells a Russian-facing
user to come back «завтра»".

The copy it set out to protect said the opposite. Four strings —
``h_ai_daily_quota_exceeded``, ``h_ai_quota_next_step`` and
``h_ai_quota_next_step_vip`` in both languages — promised "00:00 UTC",
which on the MSK production host is three hours *after* the counter has
already rolled. A user who believed the card lost three usable hours;
a user who tried at 00:05 MSK found the quota reset and reported the
limit as broken.

The fix is the copy, not the repo: the boundary follows the host zone
by design, so the text names no zone at all. Naming one would be the
same defect the moment ``TZ`` changes — and «полночь» is exactly what
legacy's «завтра» meant.

The contrast worth keeping in view is ``/voice``: its ledger day-bucket
really is UTC, its copy really says UTC (``h_voice_quota_exceeded``),
and it interpolates the computed boundary. That one is left alone.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.ai_quota_repo import _today_iso

_AI_QUOTA_KEYS = (
    "h_ai_daily_quota_exceeded",
    "h_ai_quota_next_step",
    "h_ai_quota_next_step_vip",
)


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", _AI_QUOTA_KEYS)
def test_the_ai_quota_copy_names_no_timezone(key: str, lang: str) -> None:
    body = t(key, lang, count=20, limit=20)
    assert "UTC" not in body, (
        f"{key}[{lang}] promises a UTC reset, but the counter rolls at the "
        "host's local midnight — on MSK that is three hours earlier."
    )


def test_the_counter_really_rolls_at_local_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that makes the copy wrong, pinned next to it.

    21:30 UTC is already tomorrow in Moscow. Copy that sends the user
    away until 00:00 UTC sends them away for 2.5 hours after the slot
    they were refused has already been handed back.
    """
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        assert _today_iso(datetime(2026, 6, 1, 21, 30, tzinfo=UTC)) == "2026-06-02"
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_voice_copy_keeps_its_utc_claim(lang: str) -> None:
    """Guard against a blanket de-UTC-ing sweep.

    ``/voice`` counts against the naive-UTC ledger day
    (``transactions_repo.voice_today_count``) and computes its own
    boundary, so its "UTC" is the truth.
    """
    body = t("h_voice_quota_exceeded", lang, used=5, limit=5, reset="00:00")
    assert "UTC" in body
