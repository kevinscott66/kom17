"""Regression guard: every declared callback factory has a handler (#166).

A :class:`CallbackData` subclass is one half of a contract — the other
half is a ``callback_query`` registration that answers it. Declare the
first without the second and nothing fails: the module imports, mypy is
happy, ruff is happy, and the button (if anyone ever renders one) just
falls through the whole tree into the #159 stale tail, where the user
is told the card is out of date. The card is not out of date. Nobody
ever wrote the handler.

That is exactly how ``P2pBuyStart`` survived: the design gave the order
book a «Купить #N» button that opened an amount prompt, the
implementation folded that step into the order card itself
(``p2p_order`` sets ``awaiting_buy_amount``), and the now-purposeless
factory stayed behind looking like a live wire format for a year. It
was never rendered, so it never hurt anyone — but the next one might be
rendered first and wired second, and then the failure is silent and
user-visible at the same time.

So the invariant is checked against the assembled production tree, not
against a list: whatever is declared anywhere under the package must be
handled somewhere in the tree.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any, Final

import pytest
from aiogram.filters.callback_data import CallbackData, CallbackQueryFilter

if TYPE_CHECKING:
    from aiogram import Dispatcher

pytestmark = pytest.mark.integration

#: The product package. Everything outside it — test-local factories,
#: aiogram's own — is somebody else's contract.
_PACKAGE: Final[str] = "telegram_invite_bot"

#: ``python -m telegram_invite_bot``. Importing it is harmless (the
#: ``__name__`` guard keeps ``main()`` from running) but it declares
#: nothing and importing an entry point from a test invites the kind of
#: accident that guard is the only thing preventing.
_SKIP: Final[frozenset[str]] = frozenset({"telegram_invite_bot.__main__"})


def _import_every_module() -> None:
    """Import the whole package so no factory hides in a cold module.

    Walking the package rather than trusting the dispatcher's imports is
    the point: a factory declared in a module that nothing imports is
    the most orphaned a factory can be, and it is invisible to any check
    that only looks at what the tree happened to pull in.
    """
    package = importlib.import_module(_PACKAGE)
    for info in pkgutil.walk_packages(package.__path__, prefix=_PACKAGE + "."):
        if info.name in _SKIP:
            continue
        importlib.import_module(info.name)


def _declared() -> dict[type[CallbackData], str]:
    """Every product ``CallbackData`` subclass with a prefix → its name.

    Scoped to the package on purpose. ``__subclasses__`` is global and
    the whole test session shares one interpreter, so a throwaway
    factory defined inside some other test module would otherwise show
    up here as an orphan — a failure in this file, pointing at a file
    that is not this file, depending on collection order.
    """
    found: dict[type[CallbackData], str] = {}
    stack: list[Any] = list(CallbackData.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in found:
            continue
        stack.extend(cls.__subclasses__())
        if not cls.__module__.startswith(_PACKAGE + "."):
            continue
        # aiogram sets ``__prefix__`` from the ``prefix=`` class kwarg.
        # An intermediate base declared without one is a shared field
        # set, not a wire format, and has nothing to answer.
        if getattr(cls, "__prefix__", None):
            found[cls] = f"{cls.__module__}.{cls.__qualname__}"
    return found


def _handled(dispatcher: Dispatcher) -> set[type[CallbackData]]:
    """Every factory some registration in the tree filters on.

    Scans all observers, not just ``callback_query``: a factory answered
    from an inline-query or chosen-result observer is answered.
    """
    served: set[type[CallbackData]] = set()
    stack: list[Any] = [dispatcher]
    while stack:
        router = stack.pop()
        stack.extend(router.sub_routers)
        for observer in router.observers.values():
            for handler in getattr(observer, "handlers", ()):
                for wrapper in handler.filters or ():
                    # aiogram wraps each filter in a ``CallableObject``;
                    # the filter instance itself is on ``.callback``.
                    if not isinstance(wrapper.callback, CallbackQueryFilter):
                        continue
                    factory = wrapper.callback.callback_data
                    if factory.__module__.startswith(_PACKAGE + "."):
                        served.add(factory)
    return served


def test_every_callback_factory_is_answered_somewhere(
    production_dispatcher: Dispatcher,
) -> None:
    """No declared wire format may be without a handler."""
    _import_every_module()
    declared = _declared()
    served = _handled(production_dispatcher)
    orphans = sorted(name for cls, name in declared.items() if cls not in served)
    assert orphans == [], (
        "these CallbackData factories are declared but nothing in the "
        f"production tree filters on them: {orphans} — register a "
        "callback_query handler for each, or delete the factory if the "
        "flow it belonged to was superseded (see keyboards/builders/p2p.py, "
        "where the buy-amount step moved onto the order card)"
    )


def test_the_scan_actually_sees_the_tree(production_dispatcher: Dispatcher) -> None:
    """Guard the guard: two empty sets also satisfy the test above.

    Both halves have to be non-trivially populated for the invariant to
    mean anything, and the walk has to keep finding factories that live
    outside the modules the dispatcher imports directly — ``served`` is
    derived from the tree, so anything in it that the walk missed means
    the walk, not the tree, is broken.
    """
    _import_every_module()
    declared = _declared()
    served = _handled(production_dispatcher)
    assert len(declared) > 50, f"only {len(declared)} factories found — did the walk break?"
    assert len(served) > 50, f"only {len(served)} filters found — did the unwrap break?"
    assert served <= set(declared), (
        "a filter references a factory the package walk never saw: "
        f"{sorted(f'{c.__module__}.{c.__qualname__}' for c in served - set(declared))}"
    )
