"""Resolve per-user effects (shop busters, VIP perks) into typed bundles.

The legacy ``ItemEffects`` class (``bot.py:13400``) is a 200-line
collection of unrelated lookups: VIP percent, double-daily, XP
multiplier, custom title, legend status, color nick. The new
pipeline needs them as *typed bundles* the service layer can
compose with — e.g. ``DailyService.claim`` takes a
:class:`DailyEffects`, not a bag of kwargs that grow without
bound.

This service is the thin "ask the DB which bundles apply right
now" layer. One method per consumer-shaped bundle keeps the
boundary clean — callers ask for the bundle they need and get a
fully-typed answer, no plucking individual values out of a dict.

Current scope (Stages 11-14)
----------------------------
* :meth:`resolve_daily_effects` — backs /daily. Reads
  ``double_daily`` from :class:`PrivilegesRepo` and global VIP
  percent from :class:`VipRepo`. Stage 11 ran with VIP hard-zeroed
  pending the VipRepo port; Stage 12 closed that gap by wiring the
  real read. The /daily handler port (Stage 13) is end-to-end on
  the new pipeline.
* :meth:`resolve_transfer_effects` — backs the upcoming /send port.
  Reads ``tax_discount_percent`` from :class:`VipRepo` (global VIP
  only — legacy ``get_transfer_tax_discount_percent`` at
  ``bot.py:13553`` passes no ``group_id``). Stage 14 (this revision)
  ships the resolver and its typed bundle ahead of the /send
  handler so the transfer flow lands as thin glue, mirroring how
  Stages 11-13 sequenced /daily.

Future scope
------------
* ``resolve_xp_effects`` — multiplier for games handlers.
* ``resolve_appearance_effects`` — color nick, custom title, legend
  for /profile renders.

Each of those follows the same shape: input ``user_id``, output a
frozen dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from telegram_invite_bot.services.daily_service import DailyEffects

if TYPE_CHECKING:
    from datetime import datetime

    from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo


@dataclass(frozen=True, slots=True)
class TransferEffects:
    """Per-user multipliers consumed by the /send (transfer) flow.

    Lives here (not in a yet-unwritten ``transfer_service.py``)
    because EffectsService is the producer. When the TransferService
    port lands it'll import this dataclass directly; if at that point
    a richer set of transfer-side effects emerges (e.g. a recipient-
    side VIP perk), the bundle moves next to the service that owns
    the flow, same way ``DailyEffects`` lives next to ``DailyService``.

    A frozen dataclass — not a kwargs bag — so a future field
    addition (e.g. ``waive_min_floor``) is a typed change at every
    call site, not a silently-ignored keyword.
    """

    tax_discount_percent: int = 0
    """Percent off the legacy ``COINS_TRANSFER_TAX`` rate. 50 means
    the tax is halved; 100 means it's waived entirely. Legacy formula
    (``bot.py:10247``) is ``rate * max(0, 1 - discount/100)``, so 0
    leaves the rate untouched and is the safe default.

    Sourced from :data:`VipProfile.tax_discount_percent` when the
    sender holds an active global VIP grant, else 0. Per-chat VIP
    is intentionally NOT read for transfers — legacy reads the
    global path too (``get_transfer_tax_discount_percent`` calls
    ``get_vip_profile(user_id)`` with no ``group_id``)."""


class EffectsService:
    """One method per effect bundle a downstream service consumes."""

    def __init__(self, privileges_repo: PrivilegesRepo, vip_repo: VipRepo) -> None:
        self._privileges = privileges_repo
        self._vip = vip_repo

    async def resolve_daily_effects(
        self,
        user_id: int,
        *,
        now: datetime,
    ) -> DailyEffects:
        """Build a :class:`DailyEffects` from the user's active grants.

        * ``double`` ← row exists for ``privilege_type='double_daily'``
          (presence alone is the signal — legacy stores no payload).
        * ``vip_percent`` ← :data:`VipProfile.daily_bonus_percent` if
          the user holds an active *global* VIP grant, else 0. Group-
          scoped VIP is intentionally NOT read here: /daily is a
          per-user command that doesn't know which chat it should
          apply group VIP from, and legacy reads the global path
          too for this flow (``get_daily_bonus_percent``, defined at
          ``bot.py:13545``, invokes ``get_vip_profile(user_id)`` at
          ``bot.py:13547`` with no ``group_id``).

        ``now`` is passed in (not read from the wall clock) so the
        caller's clock decision flows through — important for tests
        and for the /daily flow which already has a notion of "now"
        and wants both reads to use the same instant.
        """
        double_row = await self._privileges.get_active(user_id, "double_daily", now=now)
        vip = await self._vip.get_active_profile(user_id, now=now)
        return DailyEffects(
            vip_percent=vip.daily_bonus_percent if vip is not None else 0,
            double=double_row is not None,
        )

    async def resolve_transfer_effects(
        self,
        user_id: int,
        *,
        now: datetime,
    ) -> TransferEffects:
        """Build a :class:`TransferEffects` for the sender of a /send.

        Only ``tax_discount_percent`` for now — the legacy transfer
        flow at ``bot.py:10245`` consumes exactly that one effect,
        and packaging it as a typed bundle ahead of the handler port
        means the future :class:`TransferService.send` signature is
        ``send(from_id, to_id, amount, *, effects)`` from day one,
        not ``send(..., vip_discount=...)`` that grows kwargs every
        time we discover another effect.

        ``user_id`` is the *sender*. Recipient-side perks (none today)
        would be a separate resolver call by the handler; we don't
        bundle both sides because the recipient lookup might fail
        (banned user, missing wallet) and resolving their effects
        eagerly would waste a DB round-trip on the failure path.

        ``now`` flows through from the caller (same instant the
        TransferService uses for the balance read) — matches the
        clock-injection contract :meth:`resolve_daily_effects`
        already follows.
        """
        vip = await self._vip.get_active_profile(user_id, now=now)
        return TransferEffects(
            tax_discount_percent=vip.tax_discount_percent if vip is not None else 0,
        )

    async def consume_double_daily(self, user_id: int) -> bool:
        """Remove the one-shot ``double_daily`` buster after a claim.

        Returns whether a row was actually deleted — handlers can
        log "buster consumed" only when ``True``. Caller's
        responsibility to invoke this only on a SUCCESS outcome from
        :meth:`DailyService.claim`; otherwise a race-lost claim
        would silently burn the user's buster.
        """
        return await self._privileges.remove(user_id, "double_daily")
