"""#1663: the first ``/duel`` roll must not be published on the shared card.

``handle_duel_roll`` edits a card that lives in the group and is visible
to both seats. It used to print the value the first seat rolled while
the second seat had not yet clicked — and clicking Roll is the only act
that puts the second seat's money at risk. Reading the number first and
then deciding whether to play is strictly better than playing, at the
default best-of-1 as much as in a best-of-N, so the game the cards
advertise as fair was not.

Two pins, because either half alone can bring the leak back: the
template must not carry the placeholder, and the handler must not pass
the value. ``i18n._SafeFormat`` renders a missing placeholder literally
rather than raising, which is what makes the first pin readable.
"""

from __future__ import annotations

import inspect

import pytest

from telegram_invite_bot.handlers.duel import handle_duel_roll
from telegram_invite_bot.i18n import t


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_waiting_card_template_has_no_roll_placeholder(lang: str) -> None:
    rendered = t("h_duel_waiting_other", lang, seat_name="Игрок")
    # A placeholder the caller did not supply survives into the output
    # verbatim, so its absence here is the absence of the placeholder.
    assert "{roll}" not in rendered
    assert "Игрок" in rendered


def _waiting_card_call_args(source: str) -> str:
    """The argument list of the one ``t("h_duel_waiting_other", ...)`` call.

    Scanned with a depth counter rather than split on the first ``)``:
    the arguments contain a nested call of their own
    (``mention_html(clicker, clicker_name)``), and a naive split stops
    inside it — which is exactly wide enough to miss the argument this
    test exists to forbid.
    """
    _head, sep, tail = source.partition('"h_duel_waiting_other"')
    assert sep, "the waiting card is no longer rendered from this handler"
    depth = 1
    for index, char in enumerate(tail):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return tail[:index]
    raise AssertionError("unbalanced parentheses in the waiting-card call")


def test_the_handler_does_not_pass_the_roll_into_the_waiting_card() -> None:
    # Look only at the argument list of that one call, not the whole
    # handler — ``roll`` is a perfectly ordinary name everywhere else.
    assert "roll=" not in _waiting_card_call_args(inspect.getsource(handle_duel_roll))


def test_the_scanner_itself_would_see_the_forbidden_argument() -> None:
    # Pins the pin: a depth-aware scan is only worth having if it reads
    # past the nested call, so prove it does on a sample shaped like the
    # code it guards.
    sample = (
        'x = t(\n    "h_duel_waiting_other",\n    lang,\n'
        "    seat_name=mention_html(a, b),\n    roll=my_roll,\n)\n"
    )
    assert "roll=" in _waiting_card_call_args(sample)


def test_the_roller_still_learns_its_own_value_privately() -> None:
    # The value is not withheld from the player who rolled it: it rides
    # the toast on that player's own callback, which nobody else sees.
    source = inspect.getsource(handle_duel_roll)
    assert "h_duel_rolled_toast" in source
