"""The random source for anything that decides money.

Every game in this bot used :mod:`random`'s default generator — either
the module-level functions (``random.random()``, ``random.randint``) or
a module-level ``random.Random()`` instance. That generator is the
Mersenne Twister, and MT is *not* a one-way function: its 19937-bit
state is recoverable from enough observed output, after which every
future draw is known exactly. It was never designed to resist an
adversary; the standard library says so in ``random``'s own docs.

Why that matters here and not in a single-player toy
----------------------------------------------------
The outputs are worth money, and observing them is free:

* ``handlers.games._flip_side`` is the single coin source for BOTH the
  free ``/flip`` and the staked ``/flip 500 орёл``. Anyone can spin the
  free one as often as the throttler allows, watching the same stream
  that will later settle someone's 500-coin bet.
* ``handlers.roulette`` and ``handlers.duel`` each own a *dedicated*
  ``random.Random()``, which is worse rather than better: a dedicated
  stream has no other consumer interleaving unknown draws into it, so
  the observations an attacker collects are consecutive.
* ``DailyService`` rolls the daily bonus, ``CheckService`` rolls a
  random cheque's payout, and the inventory planner rolls a luck-item
  payout — all coin-denominated, all from the same predictable family.

None of this is a *cheap* attack, and there is no evidence anyone has
tried. It is simply the wrong primitive for the job: the cost of using
the right one is a syscall per spin, and the cost of being wrong is an
opponent who knows the next coin.

The primitive
-------------
:data:`money_rng` is a :class:`random.SystemRandom` — every draw comes
from ``os.urandom`` (getrandom(2) / ``/dev/urandom``), so there is no
state to recover and no seed to guess. It subclasses
:class:`random.Random`, so it drops into every ``rng: random.Random``
parameter in the game layer without a signature change, and the same
call sites keep taking an injected generator so tests can still pin a
deterministic ``random.Random(0)``.

Two behavioural differences, neither of which any caller relies on:
``seed()`` is a no-op and ``getstate()`` / ``setstate()`` raise
``NotImplementedError``. Reproducibility is exactly the property we are
removing, so a caller that wanted those would be the bug.

NOT for cosmetics. Picking which joke to tell, which humour category to
query, or which face a decorative die shows in an AI prompt has no
adversary and no payout; those keep the cheaper default generator and
say so at the call site.
"""

from __future__ import annotations

import random

#: The shared money generator. A module-level singleton rather than a
#: factory: ``SystemRandom`` holds no state worth isolating (every draw
#: goes straight to the OS), and one importable name is what lets the
#: regression guard say "money modules use *this*".
money_rng: random.SystemRandom = random.SystemRandom()
