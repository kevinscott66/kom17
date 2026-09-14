"""User-busy scan helper for the /cpc and /duel match flows (M-G-2).

A /cpc or /duel match lives in exactly ONE FSM session, the
challenger's — ``fsm/rps.py`` pins that choice and explains it at
length ("Variant A"). That single-session shape is precisely what
makes the naive busy guard incomplete: ``await state.get_state()`` on
the caller's own key only ever sees matches the caller STARTED, never
matches the caller was dragged into.

Two holes follow from it, and this module closes both (#1517) --
with two different scans, because the two paths do not answer the
same question:

* **Accept path** — :func:`is_user_busy`. The user clicking "Accept"
  may already be the CHALLENGER of another live match. Their own key
  does carry that state, but the accept handler opens the CHALLENGER's
  key of the match being accepted, not the clicker's, so nothing ever
  looks at it. Both seats count here: accepting is the step that puts
  a player into a match somebody else can already resolve.
* **Creation path** — :func:`is_user_an_opponent`. The user typing
  ``/cpc`` may already be the recorded OPPONENT of somebody else's
  live match. Their own key is empty — the match sits on the other
  player's key — so the ``prior is not None`` guard passes and they
  end up in two matches at once, two keyboards in flight, two stakes
  to potentially debit.

The creation path deliberately scans the OPPONENT seat only. R-FIX-008
(``tests/e2e/handlers/test_rps.py::
test_cpc_two_groups_same_user_independent_fsms``) pins that one user
may hold an independent ``/cpc`` session per chat, so "is a challenger
somewhere else" is NOT busy at creation time; the same-chat case is
already covered by the handler's own ``prior is not None`` read. Using
the two-seat scan there would revoke that regression fix.

Deliberately NOT covered by either scan: whether the TARGET of a new
challenge is busy. Both creation sites carry a comment saying why --
the worst case there is a second challenge card, which the target can
decline, and no coins move either way.

The scan duck-types over the same two storage backends the
:class:`FsmTimeoutSweeper` supports (``iter_keys()`` if present, then
``MemoryStorage.storage``). Unknown backends fall back to "skip the
guard" — losing the guard is preferable to crashing the accept path
on a backend the sweeper itself wouldn't run on either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.scheduler.fsm_sweeper import memory_storage_keys

if TYPE_CHECKING:
    from aiogram.fsm.state import State
    from aiogram.fsm.storage.base import BaseStorage, StorageKey

log = logger.bind(component="scheduler.fsm_busy")


def _state_name(state: State) -> str:
    """Return a bound ``State``'s string form.

    ``State.state`` is ``str | None``, and ``None`` only for a bare
    ``State()`` never bound to a ``StatesGroup``. Every name below is
    a bound class attribute, so the raise is unreachable; it exists
    because the alternative under ``strict`` is a
    ``frozenset[str | None]``, which would quietly admit ``None`` into
    the membership test.
    """
    name = state.state
    if name is None:
        msg = f"unbound State has no name: {state!r}"
        raise RuntimeError(msg)
    return name


# Match-bearing state names. A user counts as involved when they hold
# one of these on their own key, or are the recorded ``opponent_id``
# of a key that does.
#
# #1450: read off the real ``State`` objects, never hand-written. The
# membership test below compares strings, and a set of string LITERALS
# has no failure mode at all: rename a state in ``fsm/rps.py`` or
# ``fsm/duel.py`` and the stale spelling stays here, silently stops
# matching, and DISABLES the guard for that state — the exact hole
# this module exists to close, reopened without a single traceback.
# Touching the attribute turns that rename into an ``AttributeError``
# at import, before the bot serves one update with half a guard.
# Neither FSM module imports anything but aiogram, so this costs no
# import cycle. ``tests/unit/scheduler/test_fsm_busy.py`` now holds
# the four literals and pins them against this set, which is the half
# the derivation cannot catch: a deliberate rename also changes the
# strings ALREADY WRITTEN into the FSM store, and that has to be
# acknowledged somewhere rather than merely followed.
_BUSY_STATES: frozenset[str] = frozenset(
    _state_name(state)
    for state in (
        RpsStates.awaiting_acceptance,
        RpsStates.awaiting_moves,
        DuelStates.awaiting_acceptance,
        DuelStates.awaiting_rolls,
    )
)


async def _iter_keys(storage: BaseStorage) -> list[StorageKey]:
    """Return a snapshot of every key the storage knows about.

    Mirrors :meth:`FsmTimeoutSweeper._iter_storage_keys`: duck-typed
    ``iter_keys()`` first, ``MemoryStorage.storage`` second — through
    the shared :func:`~telegram_invite_bot.scheduler.fsm_sweeper.memory_storage_keys`,
    so both scans drop the empty records aiogram's own middleware
    leaves behind for every user the bot has ever seen (#1516).

    Returns an empty list on unknown backends: callers treat that as
    "guard not applicable", and the busy check degrades to a no-op
    rather than crashing the accept path.

    Also used by ``handlers.challenge_commands`` for its pending-
    challenge scan, which is why the underscore is a lie about the
    audience rather than about the contract — the two scanners must
    see the same key set or /accept and the busy guard disagree.
    """
    iter_keys = getattr(storage, "iter_keys", None)
    if iter_keys is not None:
        return list(await iter_keys())
    if isinstance(storage, MemoryStorage):
        return memory_storage_keys(storage)
    log.bind(storage=type(storage).__name__).debug(
        "busy scan: storage backend not supported; skipping"
    )
    return []


async def _prefetch_busy(storage: BaseStorage) -> dict[StorageKey, dict[str, Any]] | None:
    """Every match-bearing record in ONE round trip, or ``None``.

    #1491. :func:`_scan_seats` used to cost ``1 + N + M`` reads
    per accept click: one :func:`_iter_keys`, a ``get_state`` for
    every key in the store, and a ``get_data`` for every key that
    turned out to be in a match. This runs on the HOT path — one
    scan per /cpc and /duel accept, sometimes two — and under
    ``FSM_BACKEND=sqlite`` every one of those queries queues
    behind the single connection
    :class:`fsm.sqlite_storage.SQLiteStorage` holds for the whole
    process, which is the same connection the routing layer's own
    ``get_state`` uses for every update the bot receives.

    The states this scan wants are :data:`_BUSY_STATES`: fixed at
    import, four of them, never user input. A backend that offers
    ``iter_records(states)`` can answer with one statement.
    ``None`` means it does not, and the caller keeps the
    key-by-key walk — which is what :class:`MemoryStorage` gets,
    where the same reads are dict lookups and the round-trip
    count is not a cost.

    Duck-typed on the method, exactly like :func:`_iter_keys`
    above and :meth:`FsmTimeoutSweeper._prefetch_records`, so the
    two scanners keep seeing the same rows through the same
    convention. Unlike :func:`_iter_keys` this returns ``None``
    rather than ``[]`` on a backend it cannot use: an empty list
    would mean "nobody is busy" and fail the guard OPEN.
    """
    iter_records = getattr(storage, "iter_records", None)
    if iter_records is None:
        return None
    records = await iter_records(tuple(_BUSY_STATES))
    return {key: data for key, _state, data in records}


async def _scan_seats(
    storage: BaseStorage,
    *,
    user_id: int,
    exclude_key: StorageKey | None,
    challenger_seat: bool,
) -> bool:
    """Shared scan behind the two public predicates.

    ``challenger_seat`` selects whether a key whose own ``user_id`` is
    the user counts. The opponent arm (``data["opponent_id"]``) always
    does — it is the seat neither handler can see from the key it
    already holds, and therefore the whole reason this module exists.

    In its portable form the scan iterates ALL keys and reads the
    data dict of those in a match-bearing state; it is O(N) over
    the keys the backend reports.
    Under :class:`MemoryStorage` that N used to be every
    ``(chat_id, user_id)`` pair the bot had ever served, because
    aiogram's ``FSMContextMiddleware`` materialises an empty record on
    every update; :func:`memory_storage_keys` filters those out, so it
    really is live sessions now (#1516).

    #1491: that used to be the whole paragraph, and it understated
    the cost on the backend production actually runs. Under
    ``FSM_BACKEND=sqlite`` one accept click cost ``1 + N + M``
    queries: ``SQLiteStorage.iter_keys`` is a single UNBOUNDED
    ``SELECT key FROM fsm_state`` pulled through ``fetchall``, then
    ``get_state`` was one SELECT per key and ``get_data`` one more
    for each key that turned out to be in a match — all of them
    queued behind the ONE connection that backend holds for the
    whole process, the same one every routed update's own
    ``get_state`` uses. :func:`_prefetch_busy` now asks for the
    four match states in a single statement, so the SQLite path is
    ONE query and the walk below is over match rows only. The
    key-by-key form survives for backends without that method,
    :class:`MemoryStorage` among them, where every read is a dict
    lookup.

    It was free either way today — production's ``fsm.db`` holds
    zero rows, because a finished match clears its own key and
    :class:`FsmTimeoutSweeper` collects the rest — which is
    exactly why the cheap fix was worth taking before the table
    ever grows, and not a reason to have left it.

    A ``LIMIT`` was considered and REJECTED, and still is. A
    bounded scan that stops before the one key it was looking for
    answers "not busy", which is precisely the hole #1517 exists
    to close — and it would fail OPEN, silently, at the moment the
    table is largest. Bounding ``iter_keys`` itself is worse: the
    sweeper shares it, and a truncated sweep strands the sessions
    it never reaches. The upgrade path past a full match-row scan
    stays a side-index keyed by participant, which answers the
    question without any walk at all.
    """
    prefetched = await _prefetch_busy(storage)
    keys = list(prefetched) if prefetched is not None else await _iter_keys(storage)
    for key in keys:
        if exclude_key is not None and key == exclude_key:
            continue
        if prefetched is None:
            state_name = await storage.get_state(key)
            if state_name not in _BUSY_STATES:
                continue
        if challenger_seat and key.user_id == user_id:
            return True
        data = prefetched[key] if prefetched is not None else await storage.get_data(key)
        if data.get("opponent_id") == user_id:
            return True
    return False


async def is_user_busy(
    storage: BaseStorage,
    *,
    user_id: int,
    exclude_key: StorageKey | None = None,
) -> bool:
    """Return ``True`` if ``user_id`` sits in an active match, EITHER seat.

    The accept-path predicate. The challenger arm is what #1517 added:
    a clicker who started a match elsewhere used to read as free,
    because the original scan matched ``opponent_id`` only.

    ``exclude_key`` lets the caller skip the FSM key it is currently
    operating on, so a scan from inside a callback handler doesn't
    flag the very match the callback is resolving. The caller passes
    the challenger's storage key.
    """
    return await _scan_seats(
        storage,
        user_id=user_id,
        exclude_key=exclude_key,
        challenger_seat=True,
    )


async def is_user_an_opponent(
    storage: BaseStorage,
    *,
    user_id: int,
    exclude_key: StorageKey | None = None,
) -> bool:
    """Return ``True`` if ``user_id`` is the recorded opponent of a live match.

    The creation-path predicate, and deliberately narrower than
    :func:`is_user_busy`: it ignores matches the user themself
    started. R-FIX-008 pins one independent ``/cpc`` session per chat
    for the same challenger, so "challenger somewhere else" must not
    read as busy here; the caller's own key in the current chat is
    already covered by the handler's ``prior is not None`` read.
    """
    return await _scan_seats(
        storage,
        user_id=user_id,
        exclude_key=exclude_key,
        challenger_seat=False,
    )


__all__ = ["is_user_an_opponent", "is_user_busy"]
