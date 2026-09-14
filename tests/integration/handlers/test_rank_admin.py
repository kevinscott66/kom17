"""Tests for handlers/rank_admin.py — /perm, /cmdcfg, /rank (R2).

Real two-file registry (users.db + moderation.db) and a fake Message —
the same harness as tests/integration/handlers/test_rank_self.py.
Legacy anchors per the handler docstrings (/perm bot.py:42248-42317,
/cmdcfg bot.py:43125-43237, override-removal-on-default
bot.py:42840-42841).

Assertions against ``h_rankadm_*`` / ``h_cmdcfg_*`` pin the KEY rather
than its prose, so a copy edit stays a copy edit. The legacy-parity
keys (``owner_only_short``, ``your_rank``, ``rank_level_*``) are
asserted through ``t()`` formatted, because their wording is parity.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import AppEnv, Settings
from telegram_invite_bot.core.ranks import (
    ALL_PERMISSION_KEYS,
    COMMAND_CATEGORIES,
    COMMAND_ENTRIES,
    ORDERED_PERMISSION_KEYS,
    PERMISSION_CATEGORIES,
    command_entry,
)
from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase

# Imports register the tables on their metadata for create_all.
from telegram_invite_bot.db.models.rank_tables import (  # noqa: F401
    CommandRankOverride,
    RankPermissionOverride,
)
from telegram_invite_bot.db.models.users import User  # noqa: F401
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.safety import install as install_safety
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers import rank_admin
from telegram_invite_bot.handlers.rank_admin import (
    build_router,
    handle_cmdcfg,
    handle_perm,
    handle_rank,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.rank_repo import RankRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services.rank_service import clear_rank_caches
from telegram_invite_bot.utils.render import parsed_length

DEV_ID = 999_000
USER_ID = 111
TARGET_ID = 222
CHAT = -1001234567890


@pytest.fixture(autouse=True)
def _isolate_caches() -> None:
    clear_rank_caches()


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    """Two-file registry carrying the production unbounded-write guard.

    ``install_safety(AppEnv.PROD)`` is what the real
    :func:`telegram_invite_bot.db.engines.build_registry` attaches. Without
    it ``/cmdcfg reset all`` passed here while raising in production, because
    the handler wraps the repo call in a broad ``except`` and answers with a
    generic save-failure.
    """
    engines = {}
    sessions = {}
    for db, base in ((DBName.USERS, UsersBase), (DBName.MODERATION, ModerationBase)):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{db.value}.db'}")
        event.listens_for(engine.sync_engine, "before_cursor_execute")(install_safety(AppEnv.PROD))
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        engines[db] = engine
        sessions[db] = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(engines=engines, sessions=sessions)
    try:
        yield reg
    finally:
        await reg.dispose()


def make_settings() -> Settings:
    """Settings stub: only ``bot.is_developer`` is touched by R2."""
    bot_cfg = SimpleNamespace(is_developer=lambda uid: uid == DEV_ID)
    return cast("Settings", SimpleNamespace(bot=bot_cfg))


class FakeMessage:
    """Just enough Message for the rank_admin handlers."""

    def __init__(
        self,
        *,
        text: str | None = None,
        chat_type: ChatType = ChatType.PRIVATE,
        chat_id: int = CHAT,
        user_id: int | None = DEV_ID,
        reply_to: Any = None,
    ) -> None:
        self.text = text
        self.chat = SimpleNamespace(type=chat_type, id=chat_id)
        self.from_user = (
            SimpleNamespace(id=user_id, is_bot=False, full_name=f"U{user_id}")
            if user_id is not None
            else None
        )
        self.reply_to_message = reply_to
        self.replies: list[str] = []
        self.answers: list[str] = []

    async def reply(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)

    async def answer(self, text: str, **kwargs: Any) -> None:
        # Continuation pages of a paginated list: plain messages, no quote.
        self.answers.append(text)


def msg(**kwargs: Any) -> Message:
    return cast("Message", FakeMessage(**kwargs))


def reply_from(user_id: int) -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=False, full_name=f"U{user_id}")
    )


async def _permission_overrides(registry: EngineRegistry) -> dict[int, dict[str, bool]]:
    async with session_for(registry, DBName.MODERATION) as s:
        return await RankRepo(s).permission_overrides()


async def _command_overrides(registry: EngineRegistry) -> dict[str, int]:
    clear_rank_caches()  # bypass the 300s cache for fresh reads
    async with session_for(registry, DBName.MODERATION) as s:
        return await RankRepo(s).command_overrides()


# The two readers below deliberately do NOT clear first: they go through
# the 300s caches, which is the only way to see whether the handler
# invalidated them (#577).
async def _cached_matrix(registry: EngineRegistry) -> dict[int, dict[str, bool]]:
    async with session_for(registry, DBName.MODERATION) as s:
        return await RankRepo(s).merged_matrix()


async def _cached_command_overrides(registry: EngineRegistry) -> dict[str, int]:
    async with session_for(registry, DBName.MODERATION) as s:
        return await RankRepo(s).command_overrides()


async def _seed_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as s:
        await UsersRepo(s).set_rank(user_id, rank, by=DEV_ID)


# -- /perm: gate + argument validation ------------------------------------------


async def test_perm_denies_non_developer(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm list 2", user_id=USER_ID)
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("owner_only_short", "ru")]


async def test_perm_without_args_shows_usage(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "/perm list" in message.replies[0]


@pytest.mark.parametrize("rank_token", ["-1", "7", "abc", "2.5"])
async def test_perm_rejects_out_of_range_rank(registry: EngineRegistry, rank_token: str) -> None:
    message = FakeMessage(text=f"/perm list {rank_token}")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "Ранг должен быть числом" in message.replies[0]


async def test_perm_unknown_action_names_the_two_actions(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm drop 2")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "<code>list</code>" in message.replies[0]
    assert "<code>set</code>" in message.replies[0]


@pytest.mark.parametrize("text", ["/perm", "/perm set 2 can_ban"])
@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_perm_usage_carries_no_bare_angle_brackets(
    registry: EngineRegistry, text: str, lang: str
) -> None:
    """The bot sends with a global HTML parse mode, so a ``<rank>``
    placeholder in the copy is an unsupported start tag and Telegram
    refuses the whole message — the usage card answered nothing at all
    until RR-4 #45 replaced the placeholders with worked examples."""
    message = FakeMessage(text=text)
    await handle_perm(cast("Message", message), make_settings(), registry, lang)
    body = message.replies[0]
    assert "/perm list" in body
    for tag in re.findall(r"<(/?[^>]*)>", body):
        assert tag.lstrip("/") in {"b", "code", "i", "u"}, tag


# -- /perm list -----------------------------------------------------------------


async def test_perm_list_renders_the_whole_vocabulary_grouped(
    registry: EngineRegistry,
) -> None:
    message = FakeMessage(text="/perm list 2")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert len(message.replies) == 1
    body = message.replies[0]
    assert body.split("\n")[0].startswith("🛡️")
    # Every permission appears exactly once, in category order (RR-4
    # #45) — not the alphabetical wall the port inherited.
    rendered = [
        line.split("<code>")[1].removesuffix("</code>")
        for line in body.split("\n")
        if line.startswith(("✅ <code>", "❌ <code>"))
    ]
    assert rendered == list(ORDERED_PERMISSION_KEYS)
    assert len(rendered) == len(ALL_PERMISSION_KEYS)
    # Each category header sits above its own keys.
    for category, keys in PERMISSION_CATEGORIES.items():
        header = t(f"h_rankadm_cat_{category}", "ru")
        assert header in body
        assert body.index(header) < body.index(f"<code>{keys[0]}</code>")
    # Rank-2 defaults (bot.py:2611-2712): can_warn ✅, can_ban ❌.
    assert "✅ <code>can_warn</code>" in body
    assert "❌ <code>can_ban</code>" in body
    # The counter agrees with the marks it summarises.
    assert t("h_rankadm_granted", "ru", granted=body.count("✅"), total=25) in body


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_perm_list_fits_one_telegram_message(registry: EngineRegistry, lang: str) -> None:
    """25 permissions plus seven headers must stay inside the 4096-char
    single-message cap — the card is sent unsplit."""
    message = FakeMessage(text="/perm list 6")
    await handle_perm(cast("Message", message), make_settings(), registry, lang)
    assert len(message.replies[0]) < 4096


async def test_perm_list_rank_zero_is_all_denied(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm list 0")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "✅" not in message.replies[0]


# -- /perm set ------------------------------------------------------------------


async def test_perm_set_writes_override_and_list_reflects_it(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    message = FakeMessage(text="/perm set 2 can_ban on")
    await handle_perm(cast("Message", message), settings, registry, "ru")
    assert "✅ Ранг" in message.replies[0]
    assert (await _permission_overrides(registry)) == {2: {"can_ban": True}}

    listing = FakeMessage(text="/perm list 2")
    await handle_perm(cast("Message", listing), settings, registry, "ru")
    assert "✅ <code>can_ban</code>" in listing.replies[0]


async def test_perm_set_invalidates_the_cached_matrix(registry: EngineRegistry) -> None:
    """#577: ``RankRepo.set_permission`` no longer clears the cache itself.

    Clearing from inside the repo ran *before* the session middleware
    committed, so a concurrent update could refill the cache from the
    pre-commit snapshot and pin the stale cell for the full 300s TTL.
    The handler clears instead, after the ``session_for`` block exits —
    so a matrix that was already cached must still pick the new cell up
    on the very next read.
    """
    settings = make_settings()
    # Prime the cache with the pre-write value.
    assert (await _cached_matrix(registry))[2].get("can_ban", False) is False

    await handle_perm(msg(text="/perm set 2 can_ban on"), settings, registry, "ru")

    assert (await _cached_matrix(registry))[2].get("can_ban", False) is True


async def test_perm_set_off_and_rank_zero_cell(registry: EngineRegistry) -> None:
    settings = make_settings()
    off = FakeMessage(text="/perm set 5 can_warn off")
    await handle_perm(cast("Message", off), settings, registry, "ru")
    zero = FakeMessage(text="/perm set 0 can_warn вкл")  # RU toggle token
    await handle_perm(cast("Message", zero), settings, registry, "ru")
    assert (await _permission_overrides(registry)) == {
        5: {"can_warn": False},
        0: {"can_warn": True},
    }


async def test_perm_set_validates_permission_key(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm set 2 can_fly on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    body = message.replies[0]
    # The rejected key is echoed back — legacy did (bot.py:42298), the
    # port had dropped it, leaving "unknown permission" and 25 keys.
    assert "<code>can_fly</code>" in body
    # …followed by the full grouped vocabulary as the reference.
    for key in ALL_PERMISSION_KEYS:
        assert f"<code>{key}</code>" in body
    assert (await _permission_overrides(registry)) == {}


async def test_perm_set_suggests_near_misses(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm set 2 can_wran on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    body = message.replies[0]
    hint = body.split("\n\n")[1]
    assert hint.startswith("🤔")
    assert "<code>can_warn</code>" in hint


async def test_perm_set_suggests_the_prefixed_key_for_a_bare_verb(
    registry: EngineRegistry,
) -> None:
    """``/perm set 2 mute on`` — dropping the ``can_`` prefix is the
    likeliest operator slip, so it must reach a suggestion."""
    message = FakeMessage(text="/perm set 2 mute on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "<code>can_mute</code>" in message.replies[0].split("\n\n")[1]


async def test_perm_set_offers_no_suggestions_for_junk(
    registry: EngineRegistry,
) -> None:
    message = FakeMessage(text="/perm set 2 banana on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    body = message.replies[0]
    assert "🤔" not in body
    assert t("h_rankadm_perm_vocabulary", "ru") in body


async def test_perm_set_echo_is_escaped_and_clipped(registry: EngineRegistry) -> None:
    """The echoed key is user input landing in an HTML message: it must
    be escaped, and a pasted wall must not pad the reply."""
    injection = "<b>x" + "a" * 200
    message = FakeMessage(text=f"/perm set 2 {injection} on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    body = message.replies[0]
    assert "&lt;b&gt;x" in body
    assert "<b>x" not in body
    assert "a" * 40 not in body


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_perm_unknown_key_reply_fits_one_telegram_message(
    registry: EngineRegistry, lang: str
) -> None:
    message = FakeMessage(text="/perm set 2 can_wran on")
    await handle_perm(cast("Message", message), make_settings(), registry, lang)
    assert len(message.replies[0]) < 4096


async def test_perm_set_validates_toggle(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/perm set 2 can_ban maybe")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "on/off" in message.replies[0]


async def test_perm_set_accepts_can_remove_warn_spelling(
    registry: EngineRegistry,
) -> None:
    """The MATRIX spelling (can_remove_warn) must validate — it is the
    storage key; can_unwarn is the vocabulary twin (r1_api contract)."""
    message = FakeMessage(text="/perm set 3 can_remove_warn on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "✅ Ранг" in message.replies[0]


async def test_perm_set_folds_the_unwarn_vocabulary_spelling(
    registry: EngineRegistry,
) -> None:
    """#808: ``can_unwarn`` is the vocabulary twin, not a storage key.

    It validates (it is in the rendered surface) but nothing reads it —
    ``/unwarn`` is gated on ``can_remove_warn``
    (``moderation.handle_unwarn``) — so the write has to land on the
    twin. Before the fold this reply
    said "✅ Ранг" and granted nobody anything.
    """
    message = FakeMessage(text="/perm set 3 can_unwarn on")
    await handle_perm(cast("Message", message), make_settings(), registry, "ru")
    assert "✅ Ранг" in message.replies[0]
    # The echo reports what was WRITTEN, not what was typed.
    assert "can_remove_warn" in message.replies[0]
    assert (await _permission_overrides(registry)) == {3: {"can_remove_warn": True}}


async def test_perm_list_renders_the_unwarn_twin_in_agreement(
    registry: EngineRegistry,
) -> None:
    """#808: two rows, one cell — they must never disagree.

    ``can_unwarn`` has no storage of its own, so rendering it off its own
    key showed ❌ beside a granted ``can_remove_warn`` and made the fold
    above look like it had not landed.
    """
    settings = make_settings()
    await handle_perm(
        cast("Message", FakeMessage(text="/perm set 3 can_remove_warn on")),
        settings,
        registry,
        "ru",
    )
    listing = FakeMessage(text="/perm list 3")
    await handle_perm(cast("Message", listing), settings, registry, "ru")
    body = listing.replies[0]
    assert "✅ <code>can_unwarn</code>" in body
    assert "✅ <code>can_remove_warn</code>" in body
    # The counter still summarises the marks it prints, twin rows included.
    assert t("h_rankadm_granted", "ru", granted=body.count("✅"), total=25) in body


# -- /cmdcfg: gate + dispatch ----------------------------------------------------


async def test_cmdcfg_denies_non_developer(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/cmdcfg list", user_id=USER_ID)
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("owner_only_short", "ru")]


@pytest.mark.parametrize("text", ["/cmdcfg", "/cmdcfg bogus", "/cmdcfg show", "/cmdcfg set warn"])
async def test_cmdcfg_bad_invocations_show_usage(registry: EngineRegistry, text: str) -> None:
    message = FakeMessage(text=text)
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert "/cmdcfg list" in message.replies[0]


# -- /cmdcfg list / show ----------------------------------------------------------


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_cmdcfg_list_prints_the_whole_catalog(registry: EngineRegistry, lang: str) -> None:
    """RR-4 #40 — every catalog row, not only the changed ones.

    ``list`` is what an operator reads to find out what a command's
    access *is*; a command nobody has touched yet is exactly the one
    they need to look up, so "only overrides" answered the wrong
    question.
    """
    message = FakeMessage(text="/cmdcfg list")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, lang)
    body = message.replies[0]

    for entry in COMMAND_ENTRIES:
        assert f"/{entry.key} → {entry.default_rank}" in body, entry.key
    for category in COMMAND_CATEGORIES:
        assert t(f"h_cmdcfg_cat_{category}", lang) in body, category
    assert t("h_cmdcfg_all_default", lang) in body


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_cmdcfg_list_fits_one_telegram_message(registry: EngineRegistry, lang: str) -> None:
    """Every emitted message, not only the first: pagination bounds the
    catalog now (tests/unit/handlers/test_cmdcfg_list_pages.py), but a
    page that still overflowed would fail to send exactly as silently.

    Measured with ``parsed_length`` — the same thing Telegram counts and
    the renderer budgets on. The raw HTML used to be asserted as the
    stricter form; it stopped being a *true* form when #114 doubled the
    catalog, because ``<code>`` markup alone now carries the raw text
    past 4096 while the parsed card sits comfortably under it."""
    message = FakeMessage(text="/cmdcfg list")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, lang)
    pages = message.replies + message.answers
    assert pages
    assert all(parsed_length(page) < 4096 for page in pages)


async def test_cmdcfg_list_marks_only_the_overridden_row(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set warn 4")), settings, registry, "ru"
    )
    message = FakeMessage(text="/cmdcfg list")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")
    marked = [line for line in message.replies[0].split("\n") if "✏️" in line]

    # One row + the "changed by hand" footer + the legend line.
    assert [line for line in marked if line.startswith("<code>")] == [
        "<code>61) /warn → 4</code> ✏️"
    ]
    assert t("h_cmdcfg_changed", "ru", count=1) in message.replies[0]


async def test_cmdcfg_list_filters_by_category(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/cmdcfg list moderation")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    body = message.replies[0]
    assert "/ban → 2" in body
    assert "/start" not in body
    assert t("h_cmdcfg_cat_basic", "ru") not in body


async def test_cmdcfg_list_rejects_an_unknown_category(
    registry: EngineRegistry,
) -> None:
    """And names the valid ones — an operator who guessed "mod" should
    not have to read the source to find "moderation"."""
    message = FakeMessage(text="/cmdcfg list mod")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    body = message.replies[0]
    assert "mod" in body
    for category in COMMAND_CATEGORIES:
        assert f"<code>{category}</code>" in body


async def test_cmdcfg_show_carries_id_aliases_and_category(
    registry: EngineRegistry,
) -> None:
    """RR-4 #41 — the card lost the three fields that identify the row.

    ``kom_kick`` is in the alias line since #116. The card is the
    operator's answer to "what does this rank actually cover", so every
    spelling the gate resolves belongs in it — unlike the public site
    index, which hides the ``kom_`` forms as noise.
    """
    message = FakeMessage(text="/cmdcfg show /kick@SomeBot")  # alias form resolves
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [
        t(
            "h_cmdcfg_show",
            "ru",
            cmd="kick",
            id=62,
            category=t("h_cmdcfg_cat_moderation", "ru"),
            aliases="/kick, /кик, /kom_kick",
            current=2,
            default=2,
        )
    ]


async def test_cmdcfg_show_reports_the_override_as_current(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set warn 4")), settings, registry, "ru"
    )
    message = FakeMessage(text="/cmdcfg show warn")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")
    body = message.replies[0]
    assert "<b>4</b>" in body  # current
    assert "<b>2</b>" in body  # catalog default, still shown


async def test_cmdcfg_show_uncataloged_command_omits_the_missing_fields(
    registry: EngineRegistry,
) -> None:
    """A key with no catalog row is still configurable — a card
    inventing "ID: —" three times reads worse than one that simply says
    so.

    The catalog covers every registered command since #114, so the
    remaining real case is a *stale* key: an override pinned on a
    command that was renamed or dropped, which ``set`` stored and
    nothing removes. Hence a name no handler owns."""
    assert command_entry("legacy_gift") is None, (
        "fixture must stay outside the catalog — pick another stale key"
    )
    message = FakeMessage(text="/cmdcfg show legacy_gift")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [
        t("h_cmdcfg_show_uncataloged", "ru", cmd="legacy_gift", current=0, default=0)
    ]


# -- /cmdcfg: <id|cmd> references ---------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected_key"),
    [("67", "ban"), ("61", "warn"), ("100", "test_logs"), ("1", "start")],
)
async def test_cmdcfg_resolves_a_numeric_id(
    registry: EngineRegistry, ref: str, expected_key: str
) -> None:
    """``/cmdcfg set 67 3`` is the documented form (legacy
    resolve_command_entry, bot.py:42799-42801). Before RR-4 #41 the
    digits fell through as a literal key, so the override landed on a
    phantom command called "67" while /ban stayed wide open."""
    message = FakeMessage(text=f"/cmdcfg set {ref} 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert await _command_overrides(registry) == {expected_key: 3}


async def test_cmdcfg_rejects_an_id_no_command_carries(
    registry: EngineRegistry,
) -> None:
    message = FakeMessage(text="/cmdcfg set 4242 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("h_cmdcfg_unknown_cmd", "ru")]
    assert await _command_overrides(registry) == {}


@pytest.mark.parametrize(
    ("ref", "expected_key"),
    [("3", "faq"), ("16", "currency"), ("60", "relations"), ("179", "lang")],
)
async def test_cmdcfg_resolves_the_renumbered_legacy_ids(
    registry: EngineRegistry, ref: str, expected_key: str
) -> None:
    """#121: 3, 16 and 60 were shared by two rows each, so each was a
    number ``/cmdcfg`` refused rather than a number that worked. After
    the renumbering every one of them lands on a single command."""
    message = FakeMessage(text=f"/cmdcfg set {ref} 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert await _command_overrides(registry) == {expected_key: 3}


async def test_cmdcfg_asks_which_command_if_an_id_ever_collides_again(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog carries no duplicated id today
    (``test_catalog_ids_are_unique``), so the collision is injected
    here. Legacy's COMMAND_BY_ID was a plain dict comprehension and the
    last row silently won; the fail-safe must keep asking instead of
    writing an override onto a command the admin did not name."""
    faq, faq2 = command_entry("faq"), command_entry("faq2")
    assert faq is not None and faq2 is not None
    monkeypatch.setattr(rank_admin, "entries_for_id", lambda _cid: (faq, faq2))
    message = FakeMessage(text="/cmdcfg set 3 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("h_cmdcfg_ambiguous_id", "ru", id=3, cmds="/faq, /faq2")]
    assert await _command_overrides(registry) == {}


async def test_cmdcfg_reset_accepts_a_numeric_id(registry: EngineRegistry) -> None:
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set ban 5")), settings, registry, "ru"
    )
    assert await _command_overrides(registry) == {"ban": 5}
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg reset 67")), settings, registry, "ru"
    )
    assert await _command_overrides(registry) == {}


# -- /cmdcfg set -------------------------------------------------------------------


async def test_cmdcfg_set_writes_override(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/cmdcfg set warn 4")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert "минимальный ранг" in message.replies[0]
    assert await _command_overrides(registry) == {"warn": 4}


async def test_cmdcfg_set_resolves_russian_alias(registry: EngineRegistry) -> None:
    """``кик`` is a legacy catalog alias of ``kick`` (bot.py:42340-42436)."""
    message = FakeMessage(text="/cmdcfg set кик 5")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert await _command_overrides(registry) == {"kick": 5}


async def test_cmdcfg_set_to_catalog_default_removes_override(
    registry: EngineRegistry,
) -> None:
    """Legacy set_command_required_rank pops the override when the value
    equals the default (bot.py:42840-42841)."""
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set warn 4")), settings, registry, "ru"
    )
    assert await _command_overrides(registry) == {"warn": 4}
    back = FakeMessage(text="/cmdcfg set warn 2")  # warn's catalog default is 2
    await handle_cmdcfg(cast("Message", back), settings, registry, "ru")
    assert "минимальный ранг" in back.replies[0]
    assert await _command_overrides(registry) == {}


async def test_cmdcfg_set_six_reports_disabled(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/cmdcfg set warn 6")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert "отключена (ранг 6" in message.replies[0]
    assert await _command_overrides(registry) == {"warn": 6}


@pytest.mark.parametrize("rank_token", ["-1", "7", "x"])
async def test_cmdcfg_set_rejects_out_of_range_rank(
    registry: EngineRegistry, rank_token: str
) -> None:
    message = FakeMessage(text=f"/cmdcfg set warn {rank_token}")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert "Ранг должен быть числом" in message.replies[0]
    assert await _command_overrides(registry) == {}


async def test_cmdcfg_set_unknown_command_pins_itself(
    registry: EngineRegistry,
) -> None:
    """Commands absent from the legacy catalog resolve to themselves
    (command_key_for contract) so new-pipeline commands can be gated."""
    message = FakeMessage(text="/cmdcfg set vip 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert await _command_overrides(registry) == {"vip": 3}


async def test_cmdcfg_rejects_junk_command_token(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/cmdcfg set !!! 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert "Команда не найдена" in message.replies[0]


# -- /cmdcfg reset -------------------------------------------------------------------


async def test_cmdcfg_reset_single(registry: EngineRegistry) -> None:
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set warn 4")), settings, registry, "ru"
    )
    message = FakeMessage(text="/cmdcfg reset warn")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")
    assert "сброшена к доступу" in message.replies[0]
    assert await _command_overrides(registry) == {}


async def test_cmdcfg_reset_all(registry: EngineRegistry) -> None:
    settings = make_settings()
    for text in ("/cmdcfg set warn 4", "/cmdcfg set ban 5"):
        await handle_cmdcfg(cast("Message", FakeMessage(text=text)), settings, registry, "ru")
    message = FakeMessage(text="/cmdcfg reset all")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")
    assert "Все переопределения" in message.replies[0]
    assert await _command_overrides(registry) == {}


async def test_cmdcfg_set_invalidates_the_cached_overrides(
    registry: EngineRegistry,
) -> None:
    """#577, command-override half: the same caller-invalidates contract."""
    settings = make_settings()
    assert await _cached_command_overrides(registry) == {}  # primes the cache

    await handle_cmdcfg(msg(text="/cmdcfg set warn 4"), settings, registry, "ru")

    assert await _cached_command_overrides(registry) == {"warn": 4}


@pytest.mark.parametrize("target", ["warn", "all"])
async def test_cmdcfg_reset_invalidates_the_cached_overrides(
    registry: EngineRegistry, target: str
) -> None:
    """#577: both reset arms clear, and both replies moved outside the
    session block so the clear happens before the operator is told the
    override is gone."""
    settings = make_settings()
    await handle_cmdcfg(msg(text="/cmdcfg set warn 4"), settings, registry, "ru")
    assert await _cached_command_overrides(registry) == {"warn": 4}  # primes

    message = FakeMessage(text=f"/cmdcfg reset {target}")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")

    assert message.replies  # the reply survived the restructure
    assert await _cached_command_overrides(registry) == {}


# -- /rank ----------------------------------------------------------------------------


async def test_rank_self_card_uses_your_rank_key(registry: EngineRegistry) -> None:
    await _seed_rank(registry, USER_ID, 3)
    message = FakeMessage(text="/rank", user_id=USER_ID)
    await handle_rank(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("your_rank", "ru", rank=t("rank_level_3", "ru"))]


async def test_rank_unranked_user_is_level_zero(registry: EngineRegistry) -> None:
    message = FakeMessage(text="/rank", user_id=USER_ID)
    await handle_rank(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("your_rank", "ru", rank=t("rank_level_0", "ru"))]


async def test_rank_in_group_masks_developer_as_owner(
    registry: EngineRegistry,
) -> None:
    """rank_name(..., in_group=True) — legacy get_rank_name masking."""
    message = FakeMessage(text="/rank", user_id=DEV_ID, chat_type=ChatType.SUPERGROUP)
    await handle_rank(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("your_rank", "ru", rank=t("rank_level_5", "ru"))]


async def test_rank_reply_target_uses_new_key(registry: EngineRegistry) -> None:
    await _seed_rank(registry, TARGET_ID, 2)
    message = FakeMessage(
        text="/rank",
        user_id=USER_ID,
        chat_type=ChatType.SUPERGROUP,
        reply_to=reply_from(TARGET_ID),
    )
    await handle_rank(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies[0].startswith("Ранг ")


async def test_rank_reply_to_self_renders_own_card(registry: EngineRegistry) -> None:
    await _seed_rank(registry, USER_ID, 1)
    message = FakeMessage(text="/rank", user_id=USER_ID, reply_to=reply_from(USER_ID))
    await handle_rank(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("your_rank", "ru", rank=t("rank_level_1", "ru"))]


async def test_rank_ignores_arguments_and_the_help_text_says_so(
    registry: EngineRegistry,
) -> None:
    """The handler never parses arguments, so the help text must not promise it.

    The ``rank`` row in ``core.ranks.COMMAND_ENTRIES`` documents that it
    deliberately stays at catalog rank 0 because it only reads.  The
    advertised string used to say
    "grant or revoke a rank: /rank @user 3", so an ordinary user could type
    that, get their own card back, and believe a rank had been granted.  Pin
    both halves together: if someone implements the write, this fails and
    forces the help text (and the catalog rank) to be revisited.
    """
    await _seed_rank(registry, USER_ID, 1)
    message = FakeMessage(text="/rank @someone 3", user_id=USER_ID)
    await handle_rank(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("your_rank", "ru", rank=t("rank_level_1", "ru"))]
    for lang in ("ru", "en"):
        assert "@user" not in t("h_cmd_rank", lang)


# -- router factory --------------------------------------------------------------------


def test_build_router_registers_three_handlers(registry: EngineRegistry) -> None:
    router = build_router(registry, make_settings())
    assert router.name == "rank_admin"
    assert len(router.message.handlers) == 3


async def test_cmdcfg_list_surfaces_overrides_outside_the_catalog(
    registry: EngineRegistry,
) -> None:
    """``set`` accepts any well-formed key by design, so ``list`` has to
    show the ones the catalog does not carry — a typo, or a command
    since renamed. Without this section the footer counted an override
    no line displayed, and ``reset all`` was the only way to discover it
    existed."""
    assert command_entry("legacy_gift") is None, (
        "fixture must stay outside the catalog — pick another stale key"
    )
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set legacy_gift 3")),
        settings,
        registry,
        "ru",
    )
    message = FakeMessage(text="/cmdcfg list")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")
    body = "\n".join(message.replies + message.answers)
    assert t("h_cmdcfg_cat_other", "ru") in body
    assert "/legacy_gift → 3" in body
    assert t("h_cmdcfg_changed", "ru", count=1) in body


async def test_cmdcfg_list_sends_every_page_not_just_the_first(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The split is worthless if the handler still emits page one only.

    The renderer's own pagination is covered in
    ``tests/unit/handlers/test_cmdcfg_list_pages.py``; what has to be
    pinned here is the wiring — first page quotes the command, the
    continuations do not.
    """
    monkeypatch.setattr(
        rank_admin, "_render_cmdcfg_list", lambda *_, **__: ["page1", "page2", "page3"]
    )
    message = FakeMessage(text="/cmdcfg list")

    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")

    assert message.replies == ["page1"]
    assert message.answers == ["page2", "page3"]


async def test_cmdcfg_list_of_the_current_catalog_stays_a_single_reply(
    registry: EngineRegistry,
) -> None:
    """No follow-up message for the catalog as it stands today."""
    message = FakeMessage(text="/cmdcfg list")

    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")

    assert len(message.replies) == 1
    assert message.answers == []


async def test_cmdcfg_list_by_category_omits_the_outside_section(
    registry: EngineRegistry,
) -> None:
    """A filtered view answers "what is in this category" — an
    uncataloged command is in none of them."""
    settings = make_settings()
    await handle_cmdcfg(
        cast("Message", FakeMessage(text="/cmdcfg set vip 3")), settings, registry, "ru"
    )
    message = FakeMessage(text="/cmdcfg list moderation")
    await handle_cmdcfg(cast("Message", message), settings, registry, "ru")
    assert t("h_cmdcfg_cat_other", "ru") not in message.replies[0]


@pytest.mark.parametrize("ref", ["²", "٣", "1" * 4400, "1234567890123"])
async def test_cmdcfg_number_lookalikes_do_not_crash_or_pin(
    registry: EngineRegistry, ref: str
) -> None:
    """``str.isdigit()`` is true for ``"²"`` and Arabic-Indic digits
    while ``int("²")`` raises, and CPython refuses to parse an integer
    past 4300 digits — either would have surfaced as a handler
    traceback. A long ASCII run must also not fall through to the name
    branch, where it would pin an override onto a phantom key."""
    message = FakeMessage(text=f"/cmdcfg set {ref} 3")
    await handle_cmdcfg(cast("Message", message), make_settings(), registry, "ru")
    assert message.replies == [t("h_cmdcfg_unknown_cmd", "ru")]
    assert await _command_overrides(registry) == {}
