"""#1978: a crafted callback payload must not reach a database bind.

``CallbackData`` is a pydantic model, and a field declared ``int``
validates the type of what Telegram sent, not its magnitude. Telegram's
callback limit is 64 bytes, which leaves room for a 25-digit number
several times over, so ``unpack`` accepted values no SQLite column can
hold and the handler carried them straight into a ``WHERE``::

    OverflowError: Python int too large to convert to SQLite INTEGER

The damage is not authorisation — the queries still match on the
caller's own id — it is that a miss stops being a miss.
``handlers/mygroups.py`` documents an unknown group and a stranger's
group as "deliberately indistinguishable"; a 31-digit id told the two
apart by crashing on one of them.

``handlers/errors.py`` has listed ``OverflowError`` by name since #1692
so the sites without a bound would read as a distinct signal. Typed
text got its discipline then (``utils/numbers.is_int_token``); the
callback path did not, and no regression covered it.

Two guards, deliberately at different levels:

* :func:`test_every_callback_int_field_is_bounded` is the structural
  one. It walks every :class:`CallbackData` subclass the app registers
  rather than a hand-kept list, so factory number 94 cannot land
  without a bound.
* the parametrized :func:`test_an_oversized_payload_is_refused_by_the_parser`
  proves the refusal happens where it has to — inside ``unpack``, as a
  ``ValueError``, which is what ``CallbackDataFilter`` catches. An
  out-of-range tap therefore matches no handler and falls through to
  the "button expired" toast instead of an error reply.
"""

from __future__ import annotations

import importlib
import pkgutil
import warnings
from typing import Any

import pytest
from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.utils.numbers import MAX_DB_INT


def _import_every_factory_module() -> None:
    """Import the packages that declare factories, so they register."""
    for package in ("keyboards", "handlers"):
        root = importlib.import_module(f"telegram_invite_bot.{package}")
        for module in pkgutil.walk_packages(root.__path__, f"telegram_invite_bot.{package}."):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                importlib.import_module(module.name)


def _every_subclass(cls: type[CallbackData]) -> list[type[CallbackData]]:
    found: list[type[CallbackData]] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_every_subclass(sub))
    return found


def _int_fields() -> list[tuple[type[CallbackData], str, Any]]:
    _import_every_factory_module()
    out: list[tuple[type[CallbackData], str, Any]] = []
    for factory in sorted(set(_every_subclass(CallbackData)), key=lambda c: c.__qualname__):
        for name, field in factory.model_fields.items():
            if int in {field.annotation, *getattr(field.annotation, "__args__", ())}:
                out.append((factory, name, field))
    return out


def test_the_tree_still_has_callback_int_fields_to_guard() -> None:
    """Guard the guard: an import that silently failed must not read
    as "every field is bounded"."""
    assert len(_int_fields()) > 50


def test_every_callback_int_field_is_bounded() -> None:
    """Structural: no ``int`` field may be declared without a ceiling.

    The bound checked for is ``MAX_DB_INT`` — the storage limit, not a
    domain limit. A page number has no business being 10**18 either,
    but that clamp belongs at the call site and several already have
    it; what this asserts is the one bound whose absence turns a bad
    tap into a crash.
    """
    unbounded: list[str] = []
    for factory, name, field in _int_fields():
        bounds = {type(item).__name__: item for item in field.metadata}
        upper = getattr(bounds.get("Le"), "le", None)
        lower = getattr(bounds.get("Ge"), "ge", None)
        if upper != MAX_DB_INT or lower != -MAX_DB_INT:
            unbounded.append(f"{factory.__module__}.{factory.__qualname__}.{name}")
    assert not unbounded, (
        "declare these as ``DbInt`` (core/callback_fields.py) — an unbounded int "
        f"field reaches a SQLite bind as an OverflowError: {unbounded}"
    )


@pytest.mark.parametrize("digits", [25, 39, 50])
def test_an_oversized_payload_is_refused_by_the_parser(digits: int) -> None:
    """And it must be a ``ValueError``, because that is what aiogram catches.

    ``CallbackDataFilter.__call__`` wraps ``unpack`` in
    ``except (TypeError, ValueError)`` and returns ``False``. pydantic's
    ``ValidationError`` derives from ``ValueError``, so the crafted tap
    matches nothing and ``handlers/stale_callback.py`` answers it. Any
    other exception type here would surface as an error reply instead.
    """
    from telegram_invite_bot.keyboards.builders.rating import RatingNav

    payload = "ratnav:" + "9" * digits
    assert len(payload.encode()) <= 64, "the premise: Telegram would deliver this"
    with pytest.raises(ValueError):
        RatingNav.unpack(payload)


def test_an_ordinary_value_still_round_trips() -> None:
    """The bound is the storage limit, so nothing legitimate moved."""
    from telegram_invite_bot.keyboards.builders.mygroups import MyGroupsCard

    card = MyGroupsCard(group_id=-1001234567890, page=3)
    assert MyGroupsCard.unpack(card.pack()) == card
    assert MyGroupsCard.unpack(f"mygrc:{-MAX_DB_INT}:{MAX_DB_INT}").group_id == -MAX_DB_INT
