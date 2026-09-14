"""Per-game pure logic + service compositions for the games domain.

This package parallels ``services/`` but groups by *game*, not by
cross-cutting concern. Each game ships a pure outcome resolver (no
DB, no Telegram, no clock, no RNG side effects — randomness flows in
via injected ``random.Random``) plus, in a later stage, a service that
composes the resolver with :class:`EconomyRepo` for the bet/payout
writes and a handler that wires the service to aiogram.

The split mirrors the inventory_use strangler track (Stage 27 →
Stage 28 → Stage 29): land the pure logic first so the service
review is "given a resolver result, do the writes" and the handler
review is thin glue. That cadence kept Stage 28 reviewable on its
own and is what this package is structured for.

Stage 32 ships only :mod:`rps` (rock-paper-scissors) as the pilot;
future games (dice, slots, blackjack) land as sibling modules.
"""
