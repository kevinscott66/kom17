"""The limits every game in the bot shares (T-020/R9, #222).

Two of them, and they are shared for the same reason: a per-game
copy of either is a copy that drifts, and neither drifts cosmetically
— one becomes a command that pays better than its siblings, the other
becomes a cap that two commands can walk through side by side.

**The stake ceiling.**

Six games take a bet: ``/roll`` and ``/flip``
(:mod:`~telegram_invite_bot.services.stake_games_service`), ``/roulette``
(:mod:`~telegram_invite_bot.services.roulette_service`), ``/duel``
(:mod:`~.duel`), ``/cpc`` (:mod:`~.rps`) and ``/pvp_coin`` /
``/pvp_dice`` (:mod:`~telegram_invite_bot.services.pvp_service`).

Legacy capped all six at 10 000 — ``/cpc`` included. The claim this
docstring carried until #327, that ``/cpc`` allowed **100 000** and that
"one round could swing 190 000 COM", was wrong.
``rock_paper_scissors.py:664-665`` does default ``cpc_max_bet`` to
100 000, but that default is dead code: the one and only registration
overrides it, ``bot.py:21564-21565`` passing ``cpc_min_bet=DUEL_MIN_BET,
cpc_max_bet=DUEL_MAX_BET``, and ``DUEL_MAX_BET`` comes from
``bot.py:2583`` → ``10000``. R9 closed no 100 000 hole; there was none.

What R9 does buy is shape rather than a number: one ceiling instead of
six copies, with the dead 100 000 default gone so that deleting an
override can no longer resurrect it.

**What R9 lost.** Legacy's ceiling was an admin runtime knob —
``bot.py:33685-33690`` rebinds ``DUEL_MAX_BET`` and persists
``settings["duel_max_bet"]`` from an admin callback. :data:`MAX_BET` is
a module constant, so changing the ceiling now needs a deploy. Note the
knob never fully reached ``/cpc`` even in legacy: ``_deps`` stamps
``cpc_max_bet`` once at registration
(``rock_paper_scissors.py:685-686``, read back at ``:327-328``), so
``/cpc`` kept whatever ``DUEL_MAX_BET`` held at process start. Restoring
the knob is an owner decision, tracked as #327.

**Why the ceiling stays at 10 000 rather than dropping further.** The
audit row offered "lower MAX_BET, or cap absolute payout". 10 000 COM is
roughly 11 USDT — a modest maximum stake for someone who actually paid
for their balance, and the owner's real exposure is already bounded from
the other end: R6 caps lifetime payout at lifetime deposits, so no run
of luck exports more than came in. Cutting the ceiling below 10 000
would tax honest customers to buy protection the ecosystem already has.
The variance bound this leaves is explicit: no single event in the bot
can swing more than :data:`MAX_BET` × the largest multiplier (5.7, on
``/roll``).

Both numbers live here, once, for the same reason
:mod:`~.pot` holds the pot split once: six copies of a policy constant
is the shape that drifts, and a drift in a bet ceiling is not cosmetic —
it is a command that pays better than its siblings.
``tests/unit/games/test_limits.py`` pins every game against these.

**The play-rate lock.** The anti-abuse caps
(:class:`~telegram_invite_bot.services.game_limit_service.GameLimitService`
— a cooldown plus rolling per-hour and per-day counters) are counted
across *all* games at once: ``GameLimitsRepo.count_since`` and
``last_play_at`` filter on ``user_id`` alone, with no ``game ==`` clause.
So the lock that serialises one user's ``check -> play -> record`` has
to span all games too, or two commands race the one window between them
— which is exactly what happened until #222: ``handlers.games`` kept its
own registry for ``/roll`` and ``/flip`` and ``handlers.roulette`` kept
another for ``/roulette``, so a user firing ``/roll`` and ``/roulette``
together had each land in a different lock and both pass ``check``
before either ``record``. Both modules now alias :data:`PLAY_LOCKS`.

``record`` is a bare ``session.add``
(``repositories/game_limits_repo.py``), so on its own the stamp would
still be pending when the lock released and the next update through the
lock would count the *old* ``game_plays`` rows — the one-commit-wide
window filed as #222-B. Every shipped caller therefore closes it by
committing INSIDE the lock, immediately after ``record``: an ``await
checkpoint()`` right there, on both the challenge and the accept half
of every staked game.

Those ``checkpoint()`` calls are load-bearing, not defensive noise.
Deleting one as redundant — the natural reading of a lock that already
"serialises" the sequence — reopens #222-B for that command alone, and
the resulting over-play is invisible: the caps simply stop biting for a
user who fires two updates a few milliseconds apart. Nothing raises and
no existing test notices, because they all play one game at a time.

#1944: this paragraph used to name the call sites by line number, and
listed three of the nine there actually are — all three numbers stale.
``tests/regression/test_play_stamp_checkpoints.py`` now derives the
inventory from the source instead, and fails on a ``checkpoint()``
deleted or moved out of its lock. Consult it rather than a count kept
here by hand.
"""

from __future__ import annotations

from telegram_invite_bot.utils.keyed_locks import KeyedLocks

#: Smallest accepted stake, in COM. Matches legacy across every game.
MIN_BET: int = 10

#: Largest accepted stake, in COM — the ecosystem-wide ceiling.
#:
#: Raising this raises the maximum single-event swing everywhere at
#: once, which is the point: there is no longer a per-game knob to
#: forget.
MAX_BET: int = 10_000

#: Per-user lock over one user's ``check -> play -> record`` against the
#: shared anti-abuse caps (#222).
#:
#: One registry for every game that runs those caps, because the caps
#: themselves are counted across every game — see the module docstring.
#: Keyed by ``user_id``, so cross-user plays stay fully parallel; a slot
#: is a lock only while somebody holds or waits on it.
#:
#: ``handlers.games._stake_locks`` and ``handlers.roulette._play_locks``
#: are aliases of this object, kept under their old names because each
#: module reads better naming the thing it guards, and because the leak
#: regression at ``tests/e2e/handlers/test_roulette.py`` asserts on one
#: of them by name.
PLAY_LOCKS: KeyedLocks[int] = KeyedLocks()
