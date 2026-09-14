"""Regression guard: the stale-callback tail must stay a tail (#159).

``handlers/stale_callback`` is the only unfiltered ``callback_query``
handler in the bot. That is what makes it useful — it answers the taps
every real handler declined, so a button whose handler is gone stops
spinning — and it is also the only thing that could make it dangerous.
An unfiltered handler included one line too early swallows *every*
inline tap in the product: no purchase, no panel, no P2P trade, and
every one of them answered "this card is out of date". A per-feature
e2e test would not see it; each of those flows would still pass in
isolation, because in isolation the tail is not there.

So the invariants live here, all of them read off the assembled
production tree rather than off a list:

* nothing that answers a ``callback_query`` may be included after it,
* it must be genuinely unfiltered (a filter would re-open the hole),
* and the prefix set it derives for the metric must stay non-empty,
  bounded, and packed with the separator the label split assumes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from aiogram.filters.callback_data import CallbackQueryFilter

from telegram_invite_bot.handlers.stale_callback import (
    SEPARATOR,
    callback_prefixes,
)

if TYPE_CHECKING:
    from aiogram import Dispatcher, Router

pytestmark = pytest.mark.integration

#: Name given to the tail router in ``build_stale_callback_router``.
_TAIL: Final[str] = "stale_callback"

#: Guard-the-guard sample. Prefixes behind money or moderation — the
#: taps whose disappearance would be reported as "the bot is broken".
#: If the derivation ever returns an empty or truncated set, the
#: cardinality audit below would still pass over it.
_MUST_DERIVE: Final[frozenset[str]] = frozenset({"shop_buy", "p2p_order", "wd_ok", "gadm", "menu"})

#: Prometheus keeps one time series per label value for the process
#: lifetime. The derived set is ~93 today; this is a ceiling that
#: notices an accidental "label it with the whole payload" refactor,
#: not a count to keep in sync.
_MAX_PREFIXES: Final[int] = 250


def _callback_owners(router: Any, acc: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """``{router_name: {"module.func", …}}`` for callback handlers."""
    served = {} if acc is None else acc
    observer = getattr(router, "callback_query", None)
    if observer is not None:
        for handler in observer.handlers:
            fn = handler.callback
            owner = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
            served.setdefault(getattr(router, "name", "?"), set()).add(owner)
    for sub in getattr(router, "sub_routers", []):
        _callback_owners(sub, served)
    return served


@pytest.fixture
def tail(production_dispatcher: Dispatcher) -> Router:
    """The assembled tail router, found by name rather than by index.

    By name because "it comes last" is a test below, not a premise the
    other tests get to assume.
    """
    root = production_dispatcher.sub_routers[0]
    matches = [sub for sub in root.sub_routers if sub.name == _TAIL]
    assert len(matches) == 1, f"expected exactly one {_TAIL!r} router, got {len(matches)}"
    return matches[0]


def test_the_tail_is_the_last_router_that_can_match_a_callback(
    production_dispatcher: Dispatcher,
    tail: Router,
) -> None:
    """Nothing that answers a callback query may be included after it.

    aiogram walks children in include order and stops at the first
    match. This router matches everything, so any callback handler
    behind it is dead code — and "dead code" here means every inline
    button in the bot answering "this card is out of date" instead of
    doing its job.

    The errors router is the one legitimate thing after it: it observes
    the ``error`` event, never ``callback_query``, so it cannot
    intercept.
    """
    root = production_dispatcher.sub_routers[0]
    names = [sub.name for sub in root.sub_routers]
    after = root.sub_routers[names.index(_TAIL) + 1 :]
    intercepts = sorted(name for sub in after for name in _callback_owners(sub))
    assert intercepts == [], (
        "these routers are included after the stale-callback tail and "
        f"would never run: {intercepts}"
    )
    assert tail.callback_query.handlers, "the tail router registers nothing at all"


def test_the_tail_carries_no_filter(tail: Router) -> None:
    """Unfiltered is the contract, not an oversight.

    Anything narrowing it — a chat-type gate, an ``F.data`` shape, a
    state filter — leaves the queries it excluded exactly where #159
    found them: unanswered, with the button spinning. If a future edit
    genuinely needs to narrow this, it needs to explain here why the
    excluded taps are someone else's problem.
    """
    handlers = tail.callback_query.handlers
    assert len(handlers) == 1, f"expected one catch-all, got {len(handlers)}"
    assert not (handlers[0].filters or ()), (
        f"the stale-callback tail must match every query; filters found: {handlers[0].filters}"
    )
    # ``observer.filter(...)`` stashes router-level filters on the
    # observer's own inner handler; they are inherited by every
    # registration under it, so one there narrows the catch-all just as
    # effectively as one on the handler.
    assert not tail.callback_query._handler.filters, (  # noqa: SLF001
        "a router-level filter narrows the catch-all just as much"
    )
    assert not tail.sub_routers, "the tail must stay a leaf"


def test_the_tail_does_not_touch_messages(tail: Router) -> None:
    """It answers taps, nothing else.

    Included after #158's message tail, so an unfiltered *message*
    handler here would shadow that hint — and, being unfiltered, every
    ordinary sentence in every group along with it.
    """
    assert not tail.message.handlers
    assert not tail.edited_message.handlers


def test_the_prefix_set_is_derived_and_covers_the_real_ones(
    production_dispatcher: Dispatcher,
) -> None:
    """Guard the guard: an empty set satisfies the cardinality audit."""
    derived = callback_prefixes(production_dispatcher)
    assert derived >= _MUST_DERIVE, sorted(_MUST_DERIVE - derived)


def test_the_prefix_set_stays_small_enough_to_be_a_metric_label(
    production_dispatcher: Dispatcher,
) -> None:
    """Cardinality is the reason the label is an allowlist at all."""
    derived = callback_prefixes(production_dispatcher)
    assert len(derived) <= _MAX_PREFIXES, (
        f"{len(derived)} callback prefixes would each become a Prometheus "
        "time series; if the tree really grew that much, raise the ceiling "
        "deliberately rather than silently"
    )


def test_every_factory_packs_with_the_separator_the_label_splits_on(
    production_dispatcher: Dispatcher,
) -> None:
    """``prefix_label`` splits on one character; nobody may disagree.

    aiogram exposes ``__separator__`` per subclass and there is no base
    attribute to read a default from, so the constant is written out in
    the handler. A factory that overrode it would not break anything —
    its payloads would simply all count as "unknown" — but a metric
    that quietly stops distinguishing a family is worth failing a test
    over rather than discovering during an incident.
    """
    offenders = []
    stack: list[Any] = [production_dispatcher]
    while stack:
        current = stack.pop()
        stack.extend(current.sub_routers)
        for handler in current.callback_query.handlers:
            for filter_object in handler.filters or ():
                callback = filter_object.callback
                if not isinstance(callback, CallbackQueryFilter):
                    continue
                factory = callback.callback_data
                if factory.__separator__ != SEPARATOR:
                    offenders.append(f"{factory.__name__}: {factory.__separator__!r}")
    assert offenders == [], (
        f"these CallbackData factories do not pack with {SEPARATOR!r}: {sorted(set(offenders))}"
    )


def test_no_prefix_contains_the_separator(production_dispatcher: Dispatcher) -> None:
    """A prefix with a ``:`` in it could never be matched by the label.

    ``prefix_label`` takes the first field. A factory declaring
    ``prefix="shop:buy"`` would emit data whose first field is
    ``shop``, which is in no allowlist — so every stale tap on that
    family would be filed as "unknown" while looking like it was
    covered.
    """
    bad = sorted(p for p in callback_prefixes(production_dispatcher) if SEPARATOR in p)
    assert bad == [], f"prefixes containing {SEPARATOR!r} can never be labeled: {bad}"
