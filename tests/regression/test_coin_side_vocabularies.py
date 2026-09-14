"""Regression guard: the two canonical coin-side vocabularies stay apart.

This bot canonicalises "which face came up" twice, on purpose and in two
different alphabets:

* ``handlers.games`` (the ``/flip`` family) keeps legacy's Russian
  tokens — ``орёл`` / ``решка``. They are what ``_normalize_flip_guess``
  mirrors byte-for-byte from ``bot.py:17436`` and what the game log
  persists in its ``detail`` payload, so they cannot be renamed without
  rewriting history.
* ``games.pvp`` (the staked PvP coin) uses Latin ``heads`` / ``tails``,
  because those same strings are interpolated into the ``h_pvp_side_*``
  i18n family by ``handlers.pvp_stake``.

Each half is correct alone. Crossing them is the hazard: a Latin token
handed to :func:`~telegram_invite_bot.handlers.games._flip_side_label`
renders the WRONG face (its lookup is a two-way branch, so anything that
is not ``орёл`` reads as tails), and a Russian token interpolated into
``h_pvp_side_{}`` asks i18n for ``h_pvp_side_орёл``, which answers with
the raw key.

``Literal`` aliases on both sides make the crossing a mypy error. This
file is the runtime half of the same guard: it pins each vocabulary to
its alphabet, pins the two as disjoint, and pins that every canonical
value actually has the i18n key its render path will build from it — so
a future third alias, or a "harmless" rename, fails here rather than in
a group chat.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.games import pvp
from telegram_invite_bot.handlers import games as games_handler
from telegram_invite_bot.i18n import t

_FLIP_CANON = frozenset({games_handler._HEADS_CANONICAL, games_handler._TAILS_CANONICAL})
_PVP_CANON = frozenset(pvp._COIN_SIDES)


def test_flip_canon_is_the_russian_pair() -> None:
    assert {"орёл", "решка"} == _FLIP_CANON


def test_pvp_canon_is_the_latin_pair() -> None:
    assert {"heads", "tails"} == _PVP_CANON


def test_the_two_vocabularies_are_disjoint() -> None:
    """Overlap would make the crossing undetectable by inspection."""
    assert not _FLIP_CANON & _PVP_CANON


@pytest.mark.parametrize(
    "alias", sorted(games_handler._HEADS_ALIASES | games_handler._TAILS_ALIASES)
)
def test_normalize_flip_guess_only_yields_flip_canon(alias: str) -> None:
    """Every ``/flip`` alias — ``heads`` and ``eagle`` included — comes
    out Russian. The Latin ones are INPUT spellings legacy accepted, not
    canonical values, and letting one through unchanged is exactly how a
    PvP token would end up in the flip pipeline.
    """
    assert games_handler._normalize_flip_guess(alias) in _FLIP_CANON


@pytest.mark.parametrize("alias", sorted(pvp._HEADS_ALIASES | pvp._TAILS_ALIASES))
def test_normalize_side_only_yields_pvp_canon(alias: str) -> None:
    """Mirror image: the PvP parser accepts ``орёл`` but never returns it."""
    assert pvp.normalize_side(alias) in _PVP_CANON


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_flip_side_label_renders_both_faces_distinctly(lang: str) -> None:
    """A real label per face, and never the same string for both.

    ``_flip_side_label`` branches on equality with ``_HEADS_CANONICAL``,
    so a renamed constant would collapse both faces onto the tails copy
    without raising anything.
    """
    heads = games_handler._flip_side_label(games_handler._HEADS_CANONICAL, lang)
    tails = games_handler._flip_side_label(games_handler._TAILS_CANONICAL, lang)
    assert heads != tails
    assert heads not in {"h_flip_side_heads", "h_flip_side_tails"}
    assert tails not in {"h_flip_side_heads", "h_flip_side_tails"}


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("side", sorted(_PVP_CANON))
def test_pvp_side_key_exists_for_every_canonical_side(side: str, lang: str) -> None:
    """``handlers.pvp_stake`` builds this key by interpolation, so the
    family has to cover the vocabulary exactly.
    """
    key = f"h_pvp_side_{side}"
    assert t(key, lang) != key
