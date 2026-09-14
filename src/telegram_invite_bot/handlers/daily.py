"""``/daily`` handler — Stage 13 of the strangler migration.

The legacy ``cmd_daily`` (``bot.py:17929``) is a 70-line knot that
mixes five concerns: group-feature gating, auto-delete scheduling,
sender_chat rejection, the claim flow itself, and inline-keyboard
rendering. Stages 8-12 carved out a typed pipeline behind this
handler (``DailyService.claim`` → ``EffectsService.resolve_daily_effects``
→ ``EconomyService.credit`` + the SQL race guard); this handler is
now thin glue, intentionally so.

Scope (Stage 13 — narrow on purpose)
------------------------------------
* **Private chats only.** Group calls still need ``ensure_user_access``
  and ``require_group_feature`` (chat-admin opt-in), neither of which
  has been ported — so a group call is answered by the #123
  private-only refusal (the legacy bridge that once served it was
  removed in T-011).
* **Bare ``/daily``** — args do NOT match here. The multi-bot
  spelling ``/kom_daily`` does: it was registered in #115 once legacy
  stopped running, because the command catalog advertises it and an
  advertised name that answers with silence is a broken promise.
* **Sender_chat rejected** in-handler. Legacy returns a polite text;
  we do the same so anonymous-channel users get the same message.
* **No inline keyboard yet.** Legacy attaches a "back to menu" /
  "shop" pair; both buttons drive into legacy callback handlers
  that aren't ported. Surfacing them from a new handler would
  short-circuit through legacy on the next click and confuse
  ``UPDATES_TOTAL`` accounting. Keyboard ships when its callback
  destinations port.

What we DO own end-to-end
-------------------------
1. Auto-create wallet via :meth:`EconomyRepo.get_or_create` — legacy
   calls ``register_user`` for the same reason, but our repo doesn't
   take the username/first_name (those live in ``users.db``, behind
   ``UserService``). The Stage 7 ``/balance`` handler set the
   precedent: wallet auto-create from the economy DB is decoupled
   from the user profile update.
2. Resolve :class:`DailyEffects` from :class:`EffectsService`
   (vip_percent + double).
3. :meth:`DailyService.claim` with the effects.
4. On SUCCESS *and* ``effects.double``, fire
   :meth:`EffectsService.consume_double_daily` — both writes commit
   together because the middleware shares one session across both
   services. If the claim races to RACE_LOST, we never consume the
   buster. This is a DELIBERATE DIVERGENCE, not parity: legacy burned
   the buster *before* its atomic guard (``bot.py:12095``, guard at
   ``bot.py:12102-12114``) and returned on ``rowcount == 0``
   (``bot.py:12112``) without giving it back, so a lost race cost the
   user a paid item for nothing (#468).
5. Render one of three cards: success, cooldown, or NO_WALLET (rare
   defensive path — get_or_create should have created the row).

Race-loss is rendered as cooldown
---------------------------------
``ClaimOutcome.RACE_LOST`` is operationally indistinguishable from
``COOLDOWN`` for the user — a concurrent claim won. We surface it
to logs as a distinct outcome so a flood is visible to monitoring,
but the rendered text matches the cooldown card. Showing "you got
0" or "internal error" would be a worse UX for what is, from the
user's seat, exactly the same outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.daily_service import ClaimOutcome
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.numbers import format_number

log = logger.bind(component="handlers.daily")

if TYPE_CHECKING:
    from datetime import timedelta

    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.daily_service import ClaimResult, DailyService
    from telegram_invite_bot.services.effects_service import EffectsService


# Stage 20: copy lives in ``i18n/data/{ru,en}.yaml`` under the ``h_daily_*``
# namespace. We use a fresh prefix (not the legacy ``daily_*`` keys
# imported from translations.py) because the new pipeline renders HTML,
# the legacy callsites render Markdown, and an accidental shared key
# would let a future translator update for one path silently break the
# other. Two distinct prefixes make the divergence load-bearing.


def _format_wait(remaining: timedelta, *, lang: str) -> str:
    """Render ``Xh Ym`` / ``Xч Yм``. Pure for trivial testability.

    Seconds-resolution is deliberately dropped — legacy renders only
    hours+minutes (``bot.py:17979``) and exposing seconds would invite
    rapid-fire retry spam. A wait under 1 minute degrades to "<1 min"
    in both locales so the user gets a non-empty signal instead of
    ``0h 0m``. That copy lives in the YAML as ``&lt;`` — the bot sets a
    global ``parse_mode=HTML`` (di/providers.py), so a raw ``<`` would
    make Telegram reject the whole cooldown card with a 400
    ("Unsupported start tag") for the ~1 minute a day it renders.

    The duration helper stays here (not in i18n) because the math is
    locale-independent and the lookup is for the *labels* only. Pushing
    the whole "format duration in lang X" responsibility into i18n would
    invert the cost: the YAML would need a template per (hours-bucket,
    minutes-bucket) pair and Python would still have to pick one.
    """
    total = int(remaining.total_seconds())
    hours, rem = divmod(max(total, 0), 3600)
    minutes = rem // 60
    if hours == 0 and minutes == 0:
        return t("h_daily_wait_lt_minute", lang)
    return t("h_daily_wait_short", lang, hours=hours, minutes=minutes)


async def handle_daily(
    message: Message,
    economy_repo: EconomyRepo,
    daily_service: DailyService,
    effects_service: EffectsService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)

    # Anonymous sender (channel/chat-on-behalf-of) — legacy rejects with
    # ``daily_anonymous_denied`` so the channel can't farm bonuses for
    # the admin who configured it. The check stays in the handler (not
    # the service) because the service operates on a ``user_id`` and
    # has no way to inspect message metadata.
    if message.sender_chat is not None:
        await message.answer(t("h_daily_anonymous", lang))
        return

    # Stage 7's ``/balance`` set the contract: economy-side wallet
    # auto-create on first interaction, no profile fields. The response
    # language is the middleware-injected ``lang`` — the root
    # LanguageMiddleware already resolved the effective language, so we
    # no longer override it with the economy-side ``wallet.language``
    # copy (which can lag behind a /language switch).
    wallet = await economy_repo.get_or_create(tg_user.id)

    from datetime import UTC, datetime  # local import — only path that needs it

    now = datetime.now(tz=UTC)
    effects = await effects_service.resolve_daily_effects(tg_user.id, now=now)
    result: ClaimResult = await daily_service.claim(tg_user.id, effects=effects)

    if result.outcome is ClaimOutcome.SUCCESS:
        # Consume the buster ONLY on success; a race-lost claim that
        # already burned the buster would silently waste a paid shop
        # item. Legacy's invariant — pinned here at the handler edge
        # because the service intentionally doesn't know about the
        # buster lifecycle (separation of math vs. inventory).
        if effects.double:
            await effects_service.consume_double_daily(tg_user.id)
        # Wallet balance was updated mid-flow; re-read to render the
        # post-credit number rather than reusing the pre-claim value
        # (the legacy card shows the *new* balance, not the old).
        updated = await economy_repo.get(tg_user.id)
        balance = updated.balance if updated is not None else wallet.balance
        # Itemise the payout (#11): base roll + range, streak add, VIP %,
        # x2 buster — each conditional line only shows when it contributed,
        # so a non-VIP first-claim card stays clean.
        parts = [
            t(
                "h_daily_line_base",
                lang,
                base=format_number(result.base),
                min=format_number(result.base_min),
                max=format_number(result.base_max),
            )
        ]
        if result.streak_bonus > 0:
            parts.append(t("h_daily_line_streak", lang, bonus=format_number(result.streak_bonus)))
        if result.vip_bonus > 0:
            parts.append(
                t(
                    "h_daily_line_vip",
                    lang,
                    percent=result.vip_percent,
                    bonus=format_number(result.vip_bonus),
                )
            )
        if result.doubled:
            parts.append(t("h_daily_line_double", lang, bonus=format_number(result.double_bonus)))
        # #1861: ``EconomyMiddleware`` binds every economy repository to
        # ONE session, so the guard write, the credit, the achievement
        # sweep and the buster consumption above are a single
        # transaction — and ``db/engines.py`` promoted it to
        # ``BEGIN IMMEDIATE`` on the first write-headed statement. Left
        # alone it stays open across the send below, holding
        # ``economy.db`` (the busiest file in the bot) for a Telegram
        # round trip that can outlast SQLite's 5s ``busy_timeout``.
        # Worse, the middleware rolls the whole claim back if that send
        # raises — a blocked or kicked bot silently un-earned the coins
        # it had already awarded. Commit the claim first; a failed card
        # then costs only the card. Sessions are ``expire_on_commit=False``,
        # so ``balance`` and every ``result`` field stay readable after.
        if checkpoint is not None:
            await checkpoint()
        await message.answer(
            t(
                "h_daily_success",
                lang,
                breakdown="\n".join(parts),
                amount=format_number(result.amount),
                streak=result.streak,
                balance=format_number(balance),
            )
        )
        log.bind(
            uid=tg_user.id,
            amount=result.amount,
            streak=result.streak,
            double=effects.double,
            vip_percent=effects.vip_percent,
        ).info("/daily claim success")
        return

    if result.outcome in (ClaimOutcome.COOLDOWN, ClaimOutcome.RACE_LOST):
        # #12: tease the next bonus's min–max so the wait comes with a
        # carrot. Preview at the current streak (the real next-claim
        # streak depends on when they return) including any held VIP /
        # buster effects.
        preview_min, preview_max = daily_service.preview_range(
            streak=max(1, wallet.daily_streak), effects=effects
        )
        # RACE_LOST reaches here having run ``mark_daily_claimed``'s
        # guarded UPDATE, which matched zero rows but still opened the
        # write transaction — a lock over nothing, held for the whole
        # send. (Plain COOLDOWN returns before that statement and has
        # nothing to release; the checkpoint is a no-op there.)
        if checkpoint is not None:
            await checkpoint()
        await message.answer(
            t(
                "h_daily_cooldown",
                lang,
                streak=wallet.daily_streak,
                wait=_format_wait(result.cooldown_remaining, lang=lang),
                pmin=format_number(preview_min),
                pmax=format_number(preview_max),
            )
        )
        log.bind(
            uid=tg_user.id,
            outcome=result.outcome.value,
            wait_s=int(result.cooldown_remaining.total_seconds()),
        ).info("/daily claim rejected")
        return

    if result.outcome is ClaimOutcome.NEW_USER_LOCKED:
        # #1946: a distinct card, not the cooldown one. The cooldown text
        # promises the bonus "tomorrow", which for an account inside the
        # lockout window is false — and the user would keep retrying and
        # keep reading a wait that does not shrink the way it says.
        # Nothing was written on this path (the guard returns before
        # ``mark_daily_claimed``), so there is no checkpoint to take.
        await message.answer(
            t(
                "h_daily_new_user_locked",
                lang,
                wait=_format_wait(result.cooldown_remaining, lang=lang),
            )
        )
        log.bind(
            uid=tg_user.id,
            wait_s=int(result.cooldown_remaining.total_seconds()),
        ).info("/daily refused — new-account lockout")
        return

    if result.outcome is ClaimOutcome.CREDIT_FAILED:
        # The claim rolled itself back, so the day is still available —
        # say so explicitly, otherwise the user reads "not credited" as
        # "bonus lost" and writes to support.
        await message.answer(t("h_daily_credit_failed", lang))
        log.bind(uid=tg_user.id, balance=wallet.balance).warning(
            "/daily credit failed — claim rolled back, day not burned"
        )
        return

    # NO_WALLET: get_or_create above should have prevented this, but
    # surface a defensive message rather than a silent drop if some
    # future write path deletes a wallet between get_or_create and the
    # service's own ``get``.
    await message.answer(t("h_daily_no_wallet", lang))
    log.bind(uid=tg_user.id).warning(
        "/daily NO_WALLET after get_or_create — wallet vanished mid-flow"
    )


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh router + middleware per call so tests can re-wire.

    Private-chat-only at the router level. Group calls are REFUSED
    (``with_chat_type_refusal(scope="private")`` below), not passed on:
    the legacy bridge was removed in T-011 and production runs only
    this service (#469). The gate stays because legacy's ``cmd_daily``
    owned ``ensure_user_access`` + ``require_group_feature``, and
    attaching this handler to groups without an equivalent would let
    unprivileged members claim bonuses in chats whose admins explicitly
    disabled economy.
    """
    router = Router(name="daily")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    # #1946: this is the only router whose DailyService is lockout-aware,
    # because it is the only one that claims a bonus. Developers are
    # exempt exactly as in legacy (``user_id not in DEVELOPER_IDS``,
    # bot.py:12024) so the owner can still test the flow with the guard
    # switched on.
    router.message.middleware(
        EconomyMiddleware(
            registry,
            daily_new_user_lockout_hours=settings.economy.anti_abuse_new_user_no_daily_hours,
            daily_lockout_exempt_ids=settings.bot.developer_ids,
        )
    )
    router.message.register(
        handle_daily,
        # ``kom_daily``: the multi-bot spelling legacy registered and the
        # catalog still advertises — see handlers/economy.py.
        Command("daily", "kom_daily", ignore_case=True, magic=F.args.is_(None)),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
