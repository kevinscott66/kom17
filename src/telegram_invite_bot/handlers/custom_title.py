"""``custom_title`` shop-item activation — the FSM title-input step (L-21).

The 🎁 Use callback on a ``custom_title`` inventory entry can't finish in
one click: legacy prompts the user for the title text and writes the
privilege only after the next message lands (``bot.py:13847`` →
``process_custom_title`` at ``bot.py:23970``). This module ports that
second step into the new aiogram pipeline:

1. The Use callback (``handlers/shop.handle_inventory_use``) sees the
   service return ``NEEDS_TITLE_INPUT``, parks the user in
   :attr:`CustomTitleStates.awaiting_title` with the pending inventory
   ``entry_id`` stashed in FSM data, and prompts for the title.
2. :func:`handle_custom_title_text` (this module) receives the next
   free-text message, sanitizes + clamps it, consumes the inventory
   entry, writes the ``custom_title`` privilege (read back by
   ``services/vip_display.py`` for the /profile + mention renderers),
   and clears the FSM.

Atomicity / abandon safety
==========================
The inventory entry is consumed INSIDE this step (not in the Use
callback), so an abandoned flow — the user never types a title, the FSM
sweeper reclaims the state — loses nothing: the item stays unused and
the user can re-click 🎁 Use. This mirrors legacy, where
``process_custom_title`` sets ``used=1`` only after a non-empty title
arrives (``bot.py:23985``). The consume + privilege write share the
economy session the message-side EconomyMiddleware opened, so they
commit together (or roll back together on a raise) — and the handler
checkpoints that pair before clearing the FSM, which lives in a
separate store that would otherwise record a finished flow the
economy never got.

Re-consume race: a second device clicking Use while this flow is open,
or a slow double-submit, is caught by ``InventoryRepo.consume``'s
rowcount guard — exactly one consume wins; a loser sees the
"item no longer available" copy.

Sanitisation
============
The title is free-form user text rendered (HTML-escaped by the
renderer) inside /profile and mentions, so we strip the same Unicode
control characters ``handlers/nick.py`` strips (bidi overrides /
isolates, zero-width chars) BEFORE the length clamp — an all-invisible
or empty title is rejected, never stored.
"""

from __future__ import annotations

import contextlib
import html
import json
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.services.inventory_use_planner import (
    InventoryEffectKind,
    plan_effect_application,
)
from telegram_invite_bot.utils.aiogram import require_from_user

log = logger.bind(component="handlers.custom_title")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
    from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
    from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo


# Legacy ``process_custom_title`` (``bot.py:23970``) imposes no explicit
# cap, but a title renders inside the /profile privileges block and a
# mention prefix — an unbounded string would blow past Telegram's message
# budget and dominate the card. We clamp to the same 100-char budget
# ``/nick`` uses (``handlers/nick.py:NICK_MAX_LEN``), the closest
# free-form display-name analogue.
CUSTOM_TITLE_MAX_LEN = 100

# FSM data key carrying the pending inventory entry id across the
# prompt → title-message hop.
PENDING_ENTRY_FIELD = "custom_title_entry_id"

# Same control-character defence as ``handlers/nick._sanitize_nick``:
# strip bidi overrides/isolates + zero-width chars so a title can't
# visually impersonate a system string or another user, and so invisible
# padding doesn't burn the length budget. Kept as a local copy (not
# imported from nick) so the two surfaces can diverge if their threat
# models do; today they're identical.
_FORBIDDEN_TITLE_CHARS = frozenset(
    {
        *(chr(cp) for cp in range(0x202A, 0x202F)),  # bidi embeddings/overrides + PDF
        *(chr(cp) for cp in range(0x2066, 0x206A)),  # bidi isolates + PDI
        *(chr(cp) for cp in range(0x200B, 0x200E)),  # zero-width space/non-joiner/joiner
        chr(0x2060),  # word-joiner
        chr(0xFEFF),  # zero-width no-break space / BOM
    }
)


def _sanitize_title(raw: str) -> str:
    """Drop bidi-control / zero-width chars, then strip surrounding space.

    An input that is *only* control/whitespace collapses to ``""`` and is
    rejected by the caller (no empty/invisible title is ever stored),
    matching legacy's ``if not title`` guard (``bot.py:23980``).
    """
    cleaned = "".join(ch for ch in raw if ch not in _FORBIDDEN_TITLE_CHARS)
    return cleaned.strip()


async def on_expire_custom_title(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """FSM-sweeper timeout callback for ``CustomTitleStates.awaiting_title``.

    A lingering custom_title session has no money/lockout side-effect —
    the inventory entry is consumed only when a title actually arrives,
    so an abandoned flow simply leaves the item unused. We DM the user so
    a forgotten prompt doesn't sit open indefinitely, then the sweeper
    clears the state. ``lang`` is read from the stamped FSM data so the
    DM renders in the user's language without a users-table read.
    """
    lang_raw = data.get("lang")
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.user_id, t("h_item_custom_title_timeout", lang))
    log.bind(uid=key.user_id).info("custom_title FSM session expired by sweeper")


async def handle_custom_title_text(
    message: Message,
    state: FSMContext,
    inventory_repo: InventoryRepo,
    shop_items_repo: ShopItemsRepo,
    privileges_repo: PrivilegesRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Receive the user's title text → consume the entry → write the grant.

    ``lang`` comes from :class:`LanguageMiddleware` (never
    ``tg_user.language_code``). The consume + privilege write share the
    message-side economy session, committing atomically. An empty/invalid
    title is rejected WITHOUT consuming or clearing the FSM, so the user
    can retry — matching legacy's reprompt-on-empty posture.
    """
    user = require_from_user(message)
    fsm_data = await state.get_data()
    raw_entry = fsm_data.get(PENDING_ENTRY_FIELD)
    if not isinstance(raw_entry, int):
        # State data lost / malformed (e.g. storage restart mid-flow).
        # Mirror legacy's "сессия истекла, открой инвентарь заново".
        await state.clear()
        await message.reply(t("h_item_custom_title_session_lost", lang))
        return
    entry_id = raw_entry

    title = _sanitize_title(message.text or "")
    if not title:
        # Empty/invisible title — reprompt without consuming or clearing
        # so the user can try again (legacy ``bot.py:23980`` reprompts).
        await message.reply(t("h_item_custom_title_empty", lang))
        return
    title = title[:CUSTOM_TITLE_MAX_LEN]

    # Re-read the entry under the caller's id so a crafted / stale FSM
    # can't redeem someone else's entry, and so a still-present row is
    # re-classified as custom_title (defends against the catalog row
    # changing type while the prompt was open).
    detail = await inventory_repo.get_for_user(user.id, entry_id)
    if detail is None or detail.used:
        await state.clear()
        await message.reply(t("h_item_custom_title_not_found", lang))
        return
    item = await shop_items_repo.get(detail.item_id)
    now = datetime.now()  # noqa: DTZ005 — match legacy naive convention
    if item is None or plan_effect_application(item, now=now).kind is not (
        InventoryEffectKind.CUSTOM_TITLE
    ):
        await state.clear()
        await message.reply(t("h_item_custom_title_not_found", lang))
        return

    # Race-safe consume — if another click won between the read above and
    # here, this returns False and we surface "no longer available"
    # without writing the grant.
    consumed = await inventory_repo.consume(user_id=user.id, inventory_id=entry_id, now=now)
    if not consumed:
        await state.clear()
        await message.reply(t("h_item_custom_title_not_found", lang))
        return

    # The 7-day window starts NOW (when the title is set), matching
    # legacy ``process_custom_title``'s ``apply_custom_title(..., 7)``
    # called inside the title step, not at the /use click.
    spec = plan_effect_application(item, now=now).custom_title
    assert spec is not None  # planner invariant for CUSTOM_TITLE
    expires_at = spec.granted_till_from(now)
    # #1950: re-setting the SAME title extends the window; a different
    # title replaces it. The return value is what the row ends up with.
    await privileges_repo.grant_with_value(
        user_id=user.id,
        privilege_type="custom_title",
        value=json.dumps({"title": title}),
        now=now,
        duration=expires_at - now,
    )
    # The FSM lives in its own store (prod runs ``FSM_BACKEND=sqlite``),
    # so the clear below lands immediately while the consume above is
    # still uncommitted — the flow would then be marked finished in one
    # store and never recorded in the other. Committing here also moves
    # a failing commit to *before* the confirmation reply instead of
    # after the handler has already promised the title.
    if checkpoint is not None:
        await checkpoint()
    await state.clear()
    # Title is free-form user text rendered under HTML parse mode →
    # escape before interpolation (same posture as handlers/shop's
    # name escaping). The {title} placeholder lands the escaped value.
    await message.reply(t("h_item_custom_title_set", lang, title=html.escape(title)))
    log.bind(uid=user.id, entry_id=entry_id, length=len(title)).info("custom_title set via FSM")
