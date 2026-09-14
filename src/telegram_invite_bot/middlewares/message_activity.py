"""Per-message group side effects — passive earning + stats (A-03).

Legacy ran two side effects on *every* qualifying group message via its
catch-all ``handle_all_messages`` (bot.py:43775): it incremented a
per-(user, chat, day) message counter (``increment_message_count``,
bot.py:44543) and — subject to an in-process anti-spam heuristic —
credited the author one coin (``should_reward_message`` + ``add_coins``,
bot.py:43712/9695). Removing the legacy bridge dropped both: the new
pipeline counted nothing and minted nothing, so ``/top messages``, the
profile activity card, and passive earning all silently went dead.

This restores them as a single **outer** middleware on the root router,
so it runs for every message regardless of whether a command handler
claims it (an inner middleware would only fire on a matched handler —
wrong for plain chatter that no handler touches).

Parity notes vs legacy:

* **Exclusions**: bots, channel-as-sender (``sender_chat``), non-group
  chats, and command messages (``text`` starting with ``/``) are all
  skipped — commands never reached the legacy catch-all because telebot
  dispatched them to their own handlers first.
* **Content types**: only the five legacy's catch-all subscribed to —
  ``text``, ``photo``, ``video``, ``document``, ``sticker``
  (bot.py:43773). Everything else (service messages for joins, leaves
  and pins; voice, audio, video notes, polls, locations, dice, …) never
  reached ``handle_all_messages``, so legacy neither counted it nor
  stamped membership from it. ``increment_message_count`` has exactly
  one call site in legacy — bot.py:43850, inside that handler — and the
  dedicated ``new_chat_members`` / ``left_chat_member`` handlers
  (bot.py:43914/44157) do not call it. Counting service messages here
  was not merely a stats inflation: the membership stamp below would
  rewrite ``is_active = 1`` from the very message announcing that the
  member left.
* **Stats** are counted for every qualifying message, with no throttle —
  matching legacy. Failures here are swallowed: a stats write must never
  break message propagation.
* **Membership** is stamped into ``user_group_joins`` as
  ``observed_message``, exactly as legacy did from the same catch-all
  (bot.py:43833). See :meth:`MessageActivityMiddleware._record_join` for
  why this — not the join *event* — is the path that carries the data.
* **Earning** is gated globally by ``coins_enabled`` /
  ``coins_message_reward`` (legacy had NO per-group flag for this) and
  throttled per-user in process by :class:`_RewardTracker`, replicating
  the legacy cooldown / rate-cap / min-length / duplicate heuristic. The
  tracker is process-memory only (not persisted, not per-chat) — exactly
  as legacy's ``message_reward_tracker``.
* **Daily cap** (T-019) is the one place this deliberately departs from
  legacy. The legacy heuristic bounded the *rate* of earning but nothing
  over a day, so 3 rewards/minute meant 4 320 COM/day per user — enough
  to clear the withdrawal floor from a standing start, funded by no
  money at all. ``message_reward_daily_cap`` adds the missing ceiling;
  see ``docs/ECONOMY_RATE_AUDIT.md`` §2.1 and §7.1 R1. Since
  #1789 the day's running total is re-derived from the ledger
  on the first qualifying message a user sends in a process,
  so the ceiling survives a restart.
* **today** is computed in ``StatsConfig.timezone`` so the counter row
  lands on the same calendar day the read side (``/stats`` / ``/top``)
  queries — they derive ``today`` the same way.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware
from aiogram.types import Message
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.user_group_joins_repo import UserGroupJoinsRepo
from telegram_invite_bot.services.effect_gates import GroupCoinsGate
from telegram_invite_bot.services.vip_bonus import active_message_bonus
from telegram_invite_bot.services.xp_boost import active_xp_multiplier
from telegram_invite_bot.utils.economy import validate_credit_amount
from telegram_invite_bot.utils.time import db_now

if TYPE_CHECKING:
    from aiogram.types import TelegramObject

    from telegram_invite_bot.config.settings import EconomyConfig, StatsConfig
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="middlewares.message_activity")

# The exact content types legacy's catch-all subscribed to
# (bot.py:43773). Anything outside this set was dispatched elsewhere or
# dropped, and so was never counted, never stamped into
# ``user_group_joins`` and never rewarded. Values match
# ``aiogram.enums.ContentType`` one-for-one.
_LEGACY_CONTENT_TYPES = frozenset({"text", "photo", "video", "document", "sticker"})

# Letter-or-digit gate (Latin + Cyrillic). A message of pure emoji /
# punctuation earns nothing — mirrors legacy bot.py:43733.
_ALNUM = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

# Cap the per-user tracker so a busy chat can't grow it without bound.
#
# Legacy had no such cap: ``message_reward_tracker`` is an unbounded
# ``defaultdict`` (bot.py:4300-4306). The one "200" in legacy
# (bot.py:43766) trims a *per-user* map of recent texts, not the map of
# users — a comment here claimed otherwise until #224. This bound is
# new code, and it is the tightest LRU in the tree (every other one is
# 10 000 or 50 000), so nothing load-bearing may be stored inside an
# evictable value: see :meth:`_RewardTracker._roll_day` for the daily
# cap, which used to live here and no longer does.
_MAX_TRACKED_USERS = 200


@dataclass
class _UserRewardState:
    """Anti-spam bookkeeping only — nothing here may bound money.

    This object lives in an LRU that evicts at 200 users, so every field
    on it is *forgettable by design*: losing a cooldown or a duplicate
    window costs one extra reward, and the rate cap in the next minute
    re-establishes itself. The daily coin allowance used to live here
    too and is not forgettable in that sense — see #224 and
    :meth:`_RewardTracker._roll_day`; since #1789 it is not
    forgettable across a restart either.
    """

    last_reward_ts: float = 0.0
    recent_rewards: list[float] = field(default_factory=list)
    recent_texts: dict[str, float] = field(default_factory=dict)


class _RewardTracker:
    """In-process anti-spam gate for passive message earning.

    Replicates legacy ``should_reward_message`` (bot.py:43712): a
    qualifying message earns at most once per ``cooldown_sec``, no more
    than ``max_per_minute`` times in any trailing 60s, never twice for
    the same normalized text within ``duplicate_window_sec``, and only
    when the message has at least ``min_chars`` non-space characters,
    contains a letter/digit, and isn't a single repeated character.

    On top of that legacy set it enforces ``daily_cap`` — coins credited
    per user per calendar day (T-019). Legacy had no such bound; see
    ``docs/ECONOMY_RATE_AUDIT.md`` §2.1 for why its absence was the
    ecosystem's main drain.

    State is per-user and process-local — deliberately. A restart resets
    every cooldown, same as legacy, whose tracker was a module global.
    That is accepted: losing a cooldown costs one extra reward and the
    rate cap re-establishes itself within the minute.

    The day's *earnings* used to reset with them, and that was not
    tenable (#1789). The argument for it — restarts are operator-driven,
    a grinder cannot provoke one — answered the wrong question: the
    grinder does not have to provoke anything when production deploys
    land dozens of restarts on a busy day, and every one of them handed
    the full allowance back. With the cooldowns wiped by the same
    restart, the effective ceiling was the rate cap (3/min => ~4 320
    COM/day, ~29x the intended 150). The counter is therefore *seeded*
    from the ledger — see :meth:`needs_seed` / :meth:`seed`. That keeps
    the hot path a dict lookup, because the read happens once per user
    per day, not once per message.

    Eviction is a different matter and is *not* accepted: a grinder
    cannot restart the process, but 200 other people talking can empty
    the LRU, and until #224 that handed the day's allowance back to
    whoever had exhausted it. The counter therefore lives outside the
    evictable state, keyed by calendar day.
    """

    def __init__(
        self,
        *,
        min_chars: int,
        cooldown_sec: int,
        max_per_minute: int,
        duplicate_window_sec: int,
        daily_cap: int,
    ) -> None:
        self._min_chars = min_chars
        self._cooldown_sec = cooldown_sec
        self._max_per_minute = max_per_minute
        self._duplicate_window_sec = duplicate_window_sec
        self._daily_cap = daily_cap
        self._users: OrderedDict[int, _UserRewardState] = OrderedDict()
        # #224: today's earnings, deliberately NOT inside ``_users``.
        # Bounded by "people who earned coins today" and emptied whole on
        # the first call of a new day, so it needs no eviction of its own
        # — which is the point: eviction is what broke the cap.
        #
        # That bound only holds while the cap is on. With ``daily_cap``
        # <= 0 the allowance is unbounded, nothing reads this map, and
        # ``_roll_day`` is never reached — so ``take`` must not write to
        # it either, or it grows one entry per rewarded user for the
        # life of the process (#756).
        self._earned_day: str = ""
        self._earned: dict[int, int] = {}
        # #1789: users whose ledger total for ``_earned_day`` has
        # already been read back. Kept apart from ``_earned`` because
        # "seeded with zero" and "never seeded" have to be tellable
        # apart, and because ``give_back`` drops an entry that falls to
        # zero — reusing ``_earned`` as the marker would un-seed a user
        # and re-query them on their next message. Same bound and same
        # lifetime as ``_earned``: one entry per user who reached the
        # credit path today, cleared whole by ``_roll_day``.
        self._seeded: set[int] = set()

    def _content_ok(self, text: str) -> bool:
        compact = "".join(text.split())
        if len(compact) < self._min_chars:
            return False
        if _ALNUM.search(compact) is None:
            return False
        # All-same-character spam ("аааааааа" / "11111111"). Case-folded,
        # because legacy folded first: bot.py:43725 lowercases while it
        # normalises, and bot.py:43737 builds the set from that. Dropping
        # the fold (#758) let "АаАаАаАа" through with a set of two — one
        # shifted keystroke past the cheapest spam gate we have.
        return len(set(compact.lower())) != 1

    def _state(self, user_id: int) -> _UserRewardState:
        state = self._users.get(user_id)
        if state is None:
            state = _UserRewardState()
            self._users[user_id] = state
        # Refresh LRU position BEFORE any gate can bail, so a user the
        # cooldown or the rate cap is currently rejecting still counts as
        # active. Otherwise the busiest talkers — the only ones those
        # gates ever reject — would be the first evicted, and eviction
        # clears exactly the timestamps that were holding them back.
        #
        # Before #224 this line carried the daily coin cap as well; that
        # is no longer true and must not be relied on again. The cap
        # lives in ``_earned``, outside the LRU, precisely because
        # "stays hot" is a property no LRU can promise.
        self._users.move_to_end(user_id)
        return state

    def _roll_day(self, today: str) -> None:
        """Drop yesterday's earnings wholesale on the first call of a new day.

        One string comparison on the hot path, and the map never has to
        be scanned for stale entries — the whole map *is* the stale
        entries the moment the date changes.
        """
        if self._earned_day != today:
            self._earned_day = today
            self._earned.clear()
            # #1789: yesterday's seeds are stale for the same reason
            # yesterday's earnings are — the ledger has to be re-read
            # against the new day's bounds.
            self._seeded.clear()

    def _remaining_today(self, user_id: int, today: str) -> int | None:
        """Coins ``user_id`` may still earn on ``today``; ``None`` = unbounded."""
        if self._daily_cap <= 0:
            return None
        self._roll_day(today)
        return max(0, self._daily_cap - self._earned.get(user_id, 0))

    def needs_seed(self, user_id: int, today: str) -> bool:
        """Has today's ledger total for ``user_id`` still to be read back?

        ``True`` at most once per user per calendar day per process — the
        one call site answers it with a single indexed read and then
        :meth:`seed`, after which the in-memory counter carries the rest
        of the day exactly as it did before #1789.

        ``False`` while the cap is off: nothing reads ``_earned`` then,
        so a query would buy nothing and ``_seeded`` would leak an entry
        per user for the life of the process — the #756 shape, which
        ``take`` already refuses for the same reason.
        """
        if self._daily_cap <= 0:
            return False
        self._roll_day(today)
        return user_id not in self._seeded

    def seed(self, user_id: int, today: str, earned: int) -> None:
        """Prime today's booked total from the durable ledger (#1789).

        ``earned`` is what the ledger says this user has already been
        credited today; anything the current process booked since is
        kept, hence the ``max`` — a grant that is in flight right now has
        not reached the ledger yet, and taking the smaller number would
        hand its allowance back out.

        Idempotent on ``_seeded`` so a concurrent second message cannot
        re-apply a stale read on top of a fresher booking.
        """
        if self._daily_cap <= 0:
            return
        self._roll_day(today)
        if user_id in self._seeded:
            return
        self._seeded.add(user_id)
        if earned <= 0:
            # Nothing minted today: leave ``_earned`` untouched rather
            # than writing a zero, so the map keeps meaning "users who
            # actually earned" and stays as small as it was.
            return
        self._earned[user_id] = max(self._earned.get(user_id, 0), earned)

    def take(self, user_id: int, today: str, amount: int) -> int:
        """Consume up to ``amount`` from the daily allowance; return the grant.

        Clamps rather than rejects, so the last reward of the day pays the
        exact remainder instead of overshooting the cap or being dropped
        whole. Called once the true (boost-multiplied) amount is known —
        :meth:`should_reward` only knows the base reward, so it can bail
        early on an exhausted allowance but cannot do the arithmetic.

        The allowance it clamps against is the ledger's since #1789 —
        :meth:`seed` has already folded today's committed grants in — so
        a restart no longer resets it.
        """
        remaining = self._remaining_today(user_id, today)
        granted = amount if remaining is None else min(amount, remaining)
        if granted <= 0:
            return 0
        if remaining is None:
            # Cap disabled: nothing meters this map and no day-roll ever
            # clears it, so writing here would leak an entry per user
            # forever (#756).
            return granted
        # Deliberately does not touch ``_users``: since #224 recording an
        # earning no longer creates an LRU entry as a side effect, so the
        # eviction pressure this method used to add is gone with it.
        self._earned[user_id] = self._earned.get(user_id, 0) + granted
        return granted

    def give_back(self, user_id: int, today: str, amount: int) -> None:
        """Return an allowance :meth:`take` booked for a grant that never landed.

        The cap exists to bound how many coins a day can mint, and a
        refused credit mints none — so keeping the booking would charge
        the user for the bot's own refusal and, at the balance ceiling,
        silently close their whole day (#757).

        Guarded on the date: a grant taken just before midnight must not
        be refunded out of the new day's allowance. Guarded on the cap
        too, so it stays the exact inverse of :meth:`take` — which books
        nothing while the cap is off.
        """
        if self._daily_cap <= 0 or amount <= 0 or self._earned_day != today:
            return
        booked = self._earned.get(user_id)
        if booked is None:
            return
        left = booked - amount
        if left > 0:
            self._earned[user_id] = left
        else:
            self._earned.pop(user_id, None)

    def should_reward(self, user_id: int, text: str, *, now: float, today: str) -> bool:
        """Return ``True`` (and record the grant) iff this message earns."""
        if not text or text.startswith("/"):
            return False
        if not self._content_ok(text):
            return False

        # Daily allowance — checked before the cooldown bookkeeping so an
        # exhausted user costs nothing but a dict lookup for the rest of
        # the day (no economy session, no boost read). Checked before
        # ``_state`` too: an exhausted user no longer needs an LRU slot
        # to stay exhausted, so there is nothing to keep hot.
        if self._remaining_today(user_id, today) == 0:
            return False

        state = self._state(user_id)

        # Cooldown since the user's last grant.
        if now - state.last_reward_ts < self._cooldown_sec:
            return False
        # Rate cap over the trailing 60s.
        cutoff = now - 60.0
        state.recent_rewards = [ts for ts in state.recent_rewards if ts >= cutoff]
        if len(state.recent_rewards) >= self._max_per_minute:
            return False
        # Duplicate text within the window.
        normalized = " ".join(text.lower().split())
        dup_cutoff = now - self._duplicate_window_sec
        state.recent_texts = {txt: ts for txt, ts in state.recent_texts.items() if ts >= dup_cutoff}
        if normalized in state.recent_texts:
            return False

        # Grant — record state.
        state.last_reward_ts = now
        state.recent_rewards.append(now)
        state.recent_texts[normalized] = now

        # Evict oldest users if the map outgrew the cap.
        while len(self._users) > _MAX_TRACKED_USERS:
            self._users.popitem(last=False)
        return True


class MessageActivityMiddleware(BaseMiddleware):
    """Outer middleware: count + (maybe) reward every group message."""

    def __init__(
        self,
        registry: EngineRegistry,
        economy_config: EconomyConfig,
        stats_config: StatsConfig,
    ) -> None:
        self._registry = registry
        # L-54: per-group earn toggle (/modcfg coins). TTL-cached gate;
        # stats counting stays unconditional — only earning is gated.
        self._coins_gate = GroupCoinsGate(registry)
        self._economy = economy_config
        self._tz = ZoneInfo(stats_config.timezone)
        self._tracker = _RewardTracker(
            min_chars=economy_config.message_reward_min_chars,
            cooldown_sec=economy_config.message_reward_cooldown_sec,
            max_per_minute=economy_config.message_reward_max_per_minute,
            duplicate_window_sec=economy_config.message_reward_duplicate_window_sec,
            daily_cap=economy_config.message_reward_daily_cap,
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            # Side effects must never break propagation — a failed DB
            # write here would otherwise swallow the user's actual
            # command. Swallow + log, then continue.
            try:
                await self._record(event)
            except Exception:  # noqa: BLE001 — side effect, never fatal
                # #1033: ``.exception`` rather than a stringified error.
                # Everything under ``_record`` is a database write, and
                # ``str(exc)`` on an aiosqlite or SQLAlchemy failure is a
                # bare sentence with no frame in it — the one thing that
                # would say which write broke was discarded at the only
                # place it was ever caught. The binds make a recurring
                # failure attributable to a chat and an author rather than
                # a count in the log.
                author = event.from_user
                uid = None if author is None else author.id
                log.bind(uid=uid, chat_id=event.chat.id).exception(
                    "per-message activity record failed"
                )
        return await handler(event, data)

    async def _record(self, message: Message) -> None:
        chat = message.chat
        if chat.type not in ("group", "supergroup"):
            return
        author = message.from_user
        if author is None or author.is_bot:
            return
        if message.sender_chat is not None:
            return
        if message.content_type not in _LEGACY_CONTENT_TYPES:
            return
        text = message.text
        # Commands never reached the legacy catch-all (handled elsewhere):
        # skip both stats and earning for them.
        if text is not None and text.startswith("/"):
            return

        now_local = datetime.now(self._tz)
        today = now_local.date().isoformat()
        last_message = now_local.replace(tzinfo=None)

        # 1) Stats — every qualifying message, no throttle. Its own
        #    try/except for the reason step 2 states below, which used to
        #    be given to step 2 alone (#1991): this is bookkeeping in
        #    ``message_stats.db``, it sits AHEAD of both other effects,
        #    and unguarded it unwound all of ``_record`` — so a table
        #    that has nothing to do with money silently cost the author
        #    the coin. Continuing past a dead counter cannot over-pay:
        #    the reward's own ceiling is re-derived from the economy
        #    ledger (#1789), never from this counter. The tick itself is
        #    simply lost; nothing retries it.
        try:
            await self._count_message(author.id, chat.id, today=today, now=last_message)
        except Exception:  # noqa: BLE001 — bookkeeping, never fatal
            log.bind(uid=author.id, chat_id=chat.id).exception("message count failed")

        # 2) Membership — first sighting wins. Its own try/except: this is
        #    the only one of the three side effects that touches
        #    ``users.db``, and a bookkeeping failure here must not cost the
        #    author the coin step 3 is about to credit.
        try:
            await self._record_join(author.id, chat.id, title=chat.title, sent=message.date)
        except Exception:  # noqa: BLE001 — bookkeeping, never fatal
            # #1991: ``.exception`` rather than ``str(exc)``, for the
            # reason #1033 already established forty lines above — a
            # stringified aiosqlite/SQLAlchemy failure is a bare sentence
            # with no frame in it, and the binds are what make a recurring
            # failure attributable to a chat and an author.
            log.bind(uid=author.id, chat_id=chat.id).exception("group join record failed")

        # 3) Passive earning — gated + throttled.
        reward = self._economy.coins_message_reward
        if not self._economy.coins_enabled or reward <= 0:
            return
        if not await self._coins_gate.coins_enabled(chat.id):
            return
        if text is None:
            return
        if not self._tracker.should_reward(author.id, text, now=time.monotonic(), today=today):
            return
        await self._reward(author.id, reward, today=today)

    async def _count_message(
        self, user_id: int, chat_id: int, *, today: str, now: datetime
    ) -> None:
        sessionmaker = self._registry.session(DBName.MESSAGE_STATS)
        async with sessionmaker() as session:
            await MessageStatsRepo(session).increment(user_id, chat_id, today=today, now=now)
            await session.commit()

    async def _record_join(
        self, user_id: int, chat_id: int, *, title: str | None, sent: datetime | None
    ) -> None:
        """Record the author as a member of ``chat_id`` (bot.py:43833).

        Legacy stamped ``user_group_joins`` from this same catch-all on
        every ordinary group message, with ``source="observed_message"``,
        and that path produced 17 of the 24 rows the production table
        holds. The port at first kept only the ``new_chat_members``
        writer — ``_record_joins`` at ``handlers/group_events.py:1105-1138``,
        which stamps ``source="join_event"`` (:1074; legacy's own string
        there was ``"event_new_chat_member"``, bot.py:44067). Neither
        string has a single row on production, because Telegram sends no
        service message for a join via invite link in a supergroup, and
        none at all for members who predate the bot. Nothing was recorded
        between the cutover and this method, and the join-date line in
        ``profile._group_caption`` printed "—" for "messages since
        joining" to every user first seen in that window. Hence this
        writer, back on legacy's ``source="observed_message"``.

        Legacy's four remaining call sites all used
        ``observed_private_check`` (bot.py:16357/16397/39708/39935) — a
        self-heal on the /start and profile-render paths. Two of them key
        off the single global ``CHAT_ID`` this multi-group port does not
        have; the other two would stamp the render date over the strictly
        better ``first_activity_date`` the card already falls back to
        (in ``profile._group_caption``). Not mirrored — see #289.

        ``joined_at`` is the message's own timestamp, as legacy used
        (``datetime.fromtimestamp(message.date)``, bot.py:43830), carried
        into the pipeline's naive-UTC convention rather than legacy's
        naive *local* one: the read side already assumes UTC
        (``profile._group_caption``). ``record_join`` keeps the first
        sighting, so re-observing a member refreshes only the liveness
        half and never moves the recorded date forward.
        """
        joined_at = sent.astimezone(UTC).replace(tzinfo=None) if sent is not None else db_now()
        sessionmaker = self._registry.session(DBName.USERS)
        async with sessionmaker() as session:
            await UserGroupJoinsRepo(session).record_join(
                user_id,
                chat_id,
                joined_at=joined_at,
                source="observed_message",
                group_title=title,
            )
            await session.commit()

    async def _reward(self, user_id: int, amount: int, *, today: str) -> None:
        # Reject illegal amounts (non-positive or above the cap) the same
        # way every other credit path does, before opening a transaction.
        if not validate_credit_amount(amount):
            log.bind(amount=amount).warning("message reward amount rejected")
            return
        sessionmaker = self._registry.session(DBName.ECONOMY)
        async with sessionmaker() as session:
            repo = EconomyRepo(session)
            # Ensure the wallet exists (legacy add_coins registered first),
            # then atomically credit. ``credit`` returns None if the
            # post-credit balance would breach the cap — a no-op we accept.
            await repo.get_or_create(user_id)
            # #1789: re-derive the day's running total from the ledger
            # before anything is booked against it. The counter is
            # process memory, and a fresh process used to mean a fresh
            # allowance for everybody — with the cooldowns wiped by the
            # same restart, the real ceiling became the 3/min rate cap.
            # Production deploys land dozens of restarts on a busy day,
            # so a "per calendar day" cap held for minutes at a time.
            #
            # One indexed read (``idx_transactions_to``) on the first
            # qualifying message a user sends in this process, and none
            # after: ``seed`` marks the user and every later message is
            # the same dict lookup it always was. Deliberately AFTER
            # ``get_or_create`` — same session, same transaction — and
            # BEFORE the boost reads, so the query cost lands on the one
            # message that has to pay it.
            if self._tracker.needs_seed(user_id, today):
                already = await TransactionsRepo(session).message_reward_day_total(
                    user_id, day=date.fromisoformat(today), tz=self._tz
                )
                self._tracker.seed(user_id, today, already)
            # L-21 xp_boost item: an active timed boost multiplies passive
            # message earnings (legacy ItemEffects.get_xp_multiplier,
            # bot.py:13700/13841-13845). Returns 1.0 when no boost is active,
            # so non-boosted users are unchanged. TTL-cached → at most one
            # economy read per user per 30s on this hot path; reuses the open
            # session. ``now`` is AWARE and must stay that way: both readers
            # below compare it against a legacy ``time.time()`` REAL via
            # ``.timestamp()``, and on a naive value that call reads the wall
            # clock in the HOST's zone. This line used to pass db_now()
            # (naive UTC), which on the MSK production host landed 10800s in
            # the past — an expired xp_boost kept doubling message earnings
            # and an expired VIP kept paying message_bonus, for three extra
            # hours. db_now() stays correct above, where it feeds the
            # naive-UTC ``joined_at`` column rather than a .timestamp() call.
            now = datetime.now(UTC)
            boosted = round(amount * await active_xp_multiplier(session, user_id, now))
            # #490: the VIP ``message_bonus`` perk. Legacy added it on the
            # line right after the multiplication (bot.py:43841, reading
            # ItemEffects.get_message_reward_bonus at bot.py:13537-13542);
            # the port carried bot.py:43840 over and dropped :43841, so a
            # paid, advertised perk (handlers/vip.py:177) was credited to
            # nobody. Added, never multiplied: legacy applied the boost to
            # the base reward only, so the order below is load-bearing.
            # Same TTL-cached shape as the multiplier above, on the same
            # session — no extra round-trip for the non-VIP majority.
            boosted += await active_message_bonus(session, user_id, now)
            # T-019: the boost multiplies the reward, so the day's cap can
            # only be applied once the real figure exists. ``take`` clamps
            # to whatever allowance is left and books it; 0 means the user
            # is done for today and nothing is credited.
            granted = self._tracker.take(user_id, today, boosted)
            if granted <= 0:
                return
            # #1033: the credit, the ledger row and the commit all spend
            # the allowance ``take`` just booked, so every exit that mints
            # nothing has to hand it back — not only the cap breach the
            # code already knew about. A dropped connection mid-``credit``,
            # a raising ledger insert or a failing commit used to leave the
            # booking standing, and the user silently forfeited that slice
            # of the day for a failure that was never theirs: the #757
            # complaint again, reached by a different route. Hence one
            # refund site in ``finally`` rather than a branch each, and
            # ``booked`` cleared only once the grant is committed.
            #
            # ``finally`` (not ``except``) so a cancellation refunds too:
            # ``CancelledError`` is a ``BaseException``. The one ambiguous
            # case is a cancellation delivered while ``commit`` is in
            # flight, where the write may still land and the allowance is
            # returned anyway. That is at most one message reward per
            # cancelled task, and the opposite default — charging for a
            # write nobody can confirm — is the bug this is fixing.
            booked = granted
            try:
                credited = await repo.credit(user_id, granted)
                if credited is None:
                    # Cap breach: the wallet did not move, so there is
                    # nothing to book. Same audited no-op as before
                    # (SEC-1); the return value is now read only to keep
                    # the ledger from claiming a payout that never landed.
                    #
                    # No refund call here: ``booked`` is simply left
                    # standing and the ``finally`` returns it. Falling
                    # through to the commit below rather than returning is
                    # deliberate — ``get_or_create`` above may have
                    # inserted the wallet row, and that insert is worth
                    # keeping even though the credit was refused.
                    log.bind(uid=user_id, amount=granted).warning(
                        "message reward not credited (balance cap)"
                    )
                else:
                    # #225: the passive reward is the highest-volume mint in
                    # the bot and it used to write no ledger row at all, so
                    # a week of chatting showed "received: 0" in /balance's
                    # cashflow and left nothing to reconcile the coin supply
                    # against. Legacy booked it through ``add_coins``
                    # (bot.py:43842, reason "За сообщение в чате"), which
                    # always recorded the companion row at bot.py:9741-9747.
                    # ``from_id=None`` because minted coins have no payer —
                    # the same shape /daily and /promo already write.
                    await TransactionsRepo(session).record(
                        from_id=None,
                        to_id=user_id,
                        amount=granted,
                        reason="message reward",
                        type="message_reward",
                    )
                await session.commit()
                if credited is not None:
                    # Committed: the allowance is spent for real.
                    booked = 0
            finally:
                self._tracker.give_back(user_id, today, booked)
