"""WMO code tables behind ``/weather``'s condition line.

Pure table lookups — no service, no client, no transport. The freezing
precipitation codes were dropped in the port (#951), which turned
ordinary winter weather into "Нет данных" on the one line the user most
needs during it.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.services.weather_service import _WMO_EN, _WMO_RU, wmo_label


def test_ru_and_en_tables_cover_the_same_codes() -> None:
    """A code in one table and missing from the other renders as "no
    data" for exactly one language — a whole class of #951."""
    assert _WMO_RU.keys() == _WMO_EN.keys()


@pytest.mark.parametrize("code", [56, 57, 66, 67])
def test_freezing_precipitation_codes_are_labelled(code: int) -> None:
    """Legacy carried all four (bot.py:37374-37380). Without them the
    card still renders temperature and wind, so the reply looks healthy
    while the condition line reads "no data" during freezing rain.
    """
    assert wmo_label(code, "ru") != "Нет данных"
    assert wmo_label(code, "en") != "No data"


def test_every_code_carries_the_same_emoji_in_both_languages() -> None:
    """The EN table's stated contract (``weather_service.py`` above
    ``_WMO_EN``) is the SAME emoji as the RU one."""
    for code, ru_label in _WMO_RU.items():
        assert ru_label.split()[-1] == _WMO_EN[code].split()[-1], code
