"""Answering a tap that matched no callback handler (#159).

The callback-query twin of #158, with the same root cause and a worse
symptom.

#157 and #158 closed the *message* half of "the bot said nothing": a
command written in a form no handler accepted used to fall through to
the telebot monolith, and once legacy was gone that fall-through became
silence. Inline buttons have the identical hole, and there is no
equivalent of "the user retypes it" to soften it.

The assembled tree registers 107 ``callback_query`` handlers and not
one of them is a catch-all: every single registration carries either a
:class:`~aiogram.filters.callback_data.CallbackQueryFilter` (94 of
them, over 93 distinct prefixes) or a ``F.data``/state guard. So a tap
whose ``callback_data`` matches nothing reaches the end of the tree,
aiogram returns ``UNHANDLED``, and — this is the part that hurts —
``answerCallbackQuery`` is never called. Telegram does not treat that
as "no answer"; it keeps the button in its loading state for roughly
fifteen seconds and then clears it with no explanation. The user is
left with a card that looks broken.

This is not a rare path. Every one of those prefixes is reachable from
a card that outlives the state behind it:

* a panel from before a deploy that renamed or retired its prefix,
* a card whose FSM state was dropped (restart, ``/cancel``, timeout),
  so the ``StateFilter`` registrations no longer match,
* a keyboard the user scrolled back to weeks later.

``handlers/errors.py`` cannot cover it: that router registers only
``@router.error()``, an exception catcher. No handler ran, so no
exception was raised, so there is nothing for it to catch.

So the tail of the tree gets a second child, next to #158's. It has no
filter at all — that is the whole point — and it is included last, so
it can only ever be reached by a query every real handler declined. It
does two things: acknowledge, so the spinner stops immediately, and
count, so a prefix that has quietly gone stale in production shows up
as a number instead of as a support message.

One thing bounds how much traffic this tail can ever see, and it is
worth writing down because it is not obvious from the code: Telegram
refuses ``messages.getBotCallbackAnswer`` with ``DATA_INVALID`` unless
the payload really is on a button of the message being tapped. A client
therefore cannot invent ``data`` — every query that reaches here came
from a keyboard this bot itself sent at some point. That is why the
metric's label set stays honest with an allowlist rather than a
sanitiser, and why the tail cannot be used as an amplifier: the volume
is capped by real keyboards in real chats, on top of the dispatcher's
own throttle sitting above this router.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackQueryFilter

from telegram_invite_bot.i18n import t
from telegram_invite_bot.webhook.metrics import STALE_CALLBACKS

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

#: Label for a query carrying no ``data`` at all — a game callback, or
#: a button whose payload Telegram dropped. Never a real prefix.
NO_DATA_LABEL: Final = "none"

#: Label for data whose first field is not a prefix this tree
#: registers. Covers genuine junk *and* the thirteen handlers that
#: match on raw ``F.data`` instead of a :class:`CallbackData` factory
#: (``lang_*``, ``marry_*``, ``rel_*``, ``ads_*``, ``bcast_*``,
#: ``rps_*_legacy``, ``rp18_enable``): their literals are buried in
#: magic-filter operations with no reliable way to read them back, so a
#: stale tap on one of those lands here rather than under its own name.
#: Deliberate — a label set derived from something introspectable is
#: worth more than a fuller one that a filter refactor could silently
#: unbound.
UNKNOWN_LABEL: Final = "unknown"

#: The separator :class:`CallbackData` packs fields with. aiogram sets
#: ``__separator__`` on subclasses only (``__init_subclass__``), so
#: there is no base-class attribute to read it from — it is written out
#: here and checked against every factory the tree registers by
#: ``tests/regression/test_stale_callback_surface.py``. A subclass that
#: chose a different one would not break the label, only blunt it (the
#: whole payload would read as one unknown prefix), but the audit says
#: so out loud rather than leaving it to be noticed.
SEPARATOR: Final = ":"

#: Telegram refusals that describe an ordinary fact about the tap
#: rather than a bug in us: the query aged out of its ~15 s answer
#: window, or its id no longer resolves. Both are *expected* here — a
#: card old enough to have a dead button is a card the user came back
#: to — which is exactly why the swallow is spelled out instead of a
#: bare ``except TelegramBadRequest``: a 200-character overrun in the
#: copy is also a ``TelegramBadRequest``, and that one must stay loud
#: (#46, #100). Matched as substrings because the API prefixes them
#: with ``Bad Request: `` and appends detail.
BENIGN_ANSWER_REJECTS: tuple[str, ...] = (
    "query is too old",
    "query id is invalid",
    "QUERY_ID_INVALID",
)


def callback_prefixes(router: Router) -> frozenset[str]:
    """Every :class:`CallbackData` prefix the tree can route.

    Walked off the assembled router for the same reason #158's word
    list is: a prefix added, renamed or retired anywhere changes this
    set in the same edit, and nobody has to keep a second copy current.

    Used only to bound the metric label — see :func:`prefix_label`. It
    is *not* a match list: this router answers every unmatched query,
    including ones whose prefix is unknown to it.
    """
    prefixes: set[str] = set()
    stack = [router]
    while stack:
        current = stack.pop()
        stack.extend(current.sub_routers)
        for handler in current.callback_query.handlers:
            for filter_object in handler.filters or ():
                callback = filter_object.callback
                if isinstance(callback, CallbackQueryFilter):
                    prefixes.add(callback.callback_data.__prefix__)
    return frozenset(prefixes)


def prefix_label(data: str | None, known: frozenset[str]) -> str:
    """Bound the metric label to ``known`` plus two fixed sentinels.

    ``callback_data`` is attacker-adjacent in the only sense that
    matters to Prometheus: it is a string that arrives from outside, and
    a counter labeled with it verbatim is an unbounded time series — a
    slow memory leak in the process and an expensive one in whatever
    scrapes it. So the first field is used as the label *only* if the
    tree actually registers it, and everything else collapses to
    :data:`UNKNOWN_LABEL`. Worst case the series count is 93 + 2.
    """
    if data is None:
        return NO_DATA_LABEL
    head = data.split(SEPARATOR, 1)[0]
    return head if head in known else UNKNOWN_LABEL


def build_stale_callback_router(tree: Router) -> Router:
    """A router that acknowledges any callback query nothing else took.

    Include it *after* every real callback handler — aiogram walks
    routers in include order, so a filter that would have matched
    already did. Call it with the assembled root before including the
    result: the prefix walk must not see this router's own
    registration, though as it registers no ``CallbackData`` filter
    that is a matter of hygiene rather than correctness.
    """
    known = callback_prefixes(tree)

    async def handle_stale_callback(callback: CallbackQuery, lang: str) -> None:
        """Stop the spinner and record which prefix went stale.

        The reply is a toast, not an alert: the card is old, which is
        an ordinary fact about a chat that moved on, not something
        worth a modal. It says nothing about *what* the button was —
        the raw ``callback_data`` never reaches the user, so a payload
        carrying an id or an amount cannot leak into a screenshot — and
        it needs no HTML escaping either, since ``answerCallbackQuery``
        has no parse mode.

        An aged-out query is swallowed rather than raised — see
        :data:`BENIGN_ANSWER_REJECTS` for which refusals and why. The
        error router already treats those as benign, so letting them
        through would change nothing the user sees; it would only spend
        a log line and a ``BenignReject`` on the most expected outcome
        this handler has. Anything else still propagates.
        """
        STALE_CALLBACKS.labels(prefix=prefix_label(callback.data, known)).inc()
        try:
            await callback.answer(t("h_stale_card", lang))
        except TelegramBadRequest as exc:
            if not any(m in str(exc) for m in BENIGN_ANSWER_REJECTS):
                raise

    router = Router(name="stale_callback")
    # No filter, by design: "everything the tree declined" is the
    # contract, and any filter here would re-open the hole for whatever
    # it excluded. ``CallbackQuery.from_user`` is non-optional in the
    # Bot API, so unlike the message tail this one needs no author
    # guard.
    router.callback_query.register(handle_stale_callback)
    return router
