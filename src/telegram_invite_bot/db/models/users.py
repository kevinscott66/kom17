"""SQLAlchemy mapping for the legacy ``users.users`` table.

Stage 4 only reads/writes ``user_id, username, first_name, last_name,
language_code, is_premium, joined_date, last_seen, last_active`` — but
every other column from the prod schema is declared as well so that
``Base.metadata.create_all`` (used in tests) produces a schema-compatible
table. Mismatches would mean SQLAlchemy round-trips re-insert rows the
legacy queries can't see, or vice versa.

Source of truth: ``docs/prod_schemas.sql`` (dumped from
the production host in Stage 2). Keep these two in sync.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.utils.time import db_now


class User(UsersBase):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    language_code: Mapped[str | None] = mapped_column(String, nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, nullable=True)

    joined_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_active: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    messages_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    commands_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    warnings_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    bans_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    kicks_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    mutes_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)

    data: Mapped[str | None] = mapped_column(Text, nullable=True)

    group_joined_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    group_join_source: Mapped[str | None] = mapped_column(String, nullable=True)
    welcome_dm_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admin_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    new_member_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=True)

    __table_args__ = (
        Index("idx_users_username", "username"),
        Index("idx_users_last_seen", "last_seen"),
    )


class UserGroupJoin(UsersBase):
    """Per-``(user, chat)`` membership record — when the user joined.

    The ``users.group_joined_date`` / ``group_join_source`` columns above
    are the older, single-group version of the same idea: one date per
    user, implicitly about the configured main chat. Legacy read this
    table first and only fell back to those columns when the chat asked
    about *was* the main one (``bot.py:44344-44362``) — the fallback is
    meaningless for any other group, since the column has no chat.

    Source of truth: ``docs/prod_schemas.sql:188``. ``left_at`` /
    ``is_active`` exist because the primary key is ``(user_id, chat_id)``:
    a user who leaves and rejoins keeps one row, flagged rather than
    duplicated, so ``joined_at`` stays the *first* time we saw them.
    """

    __tablename__ = "user_group_joins"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Legacy writes 1/0 and leaves old rows NULL, so this is a nullable
    # int rather than a Boolean — "NULL means active" is a real state in
    # prod data and a NOT NULL Boolean would refuse to load those rows.
    is_active: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=1, server_default="1"
    )

    __table_args__ = (
        Index("idx_user_group_joins_chat", "chat_id"),
        Index("idx_user_group_joins_active", "is_active"),
    )


class GroupSettings(UsersBase):
    """Per-group settings (welcome text, rules, moderation flags, …).

    Stage 22 needs only ``rules`` to back ``/rules``; all the other
    legacy columns (filter_words, mute_duration, rp_* …) keep living
    in the prod schema unmodelled — they belong to other features
    that haven't been ported yet, and declaring stubs here would
    misleadingly suggest the new pipeline owns them.

    The prod schema (``docs/prod_schemas.sql:223``) has ~30 columns;
    we model only the two this stage reads. ``Base.metadata.create_all``
    in tests builds a 2-column table, which is fine because tests
    seed it themselves; in prod the full table already exists from
    legacy migrations and SQLAlchemy just SELECTs the columns it
    knows about. The day another handler needs another column, we
    extend this model — additive only, never mutate prod schema
    from here.
    """

    __tablename__ = "group_settings"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rules: Mapped[str | None] = mapped_column(Text, nullable=True)

    # L-70 voice-message Speech-To-Text columns. These ALREADY exist in
    # prod from legacy (``bot.py:7771,7800-7835`` add them via ``ALTER
    # TABLE … ADD COLUMN`` at startup); migration ``0005_voice_transcription``
    # adds them to fresh/test DBs only. We model the six columns the new
    # transcription pipeline actually reads/writes; legacy also keeps
    # ``transcription_target``-adjacent ``transcription_model`` /
    # ``transcription_device`` columns (faster-whisper knobs) which the
    # OpenAI-Whisper port no longer consults — they stay unmodelled here
    # (additive-only policy, see the class docstring). Server-side defaults
    # match the legacy ALTER defaults so a fresh row built by either side
    # reads identically.
    voice_transcription: Mapped[int] = mapped_column(
        Integer, nullable=True, server_default="0", default=0
    )
    transcription_target: Mapped[str] = mapped_column(
        Text, nullable=True, server_default="chat", default="chat"
    )
    transcription_language: Mapped[str] = mapped_column(
        Text, nullable=True, server_default="ru", default="ru"
    )
    transcription_log_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_delete_voice: Mapped[int] = mapped_column(
        Integer, nullable=True, server_default="0", default=0
    )
    transcription_only_for_admins: Mapped[int] = mapped_column(
        Integer, nullable=True, server_default="0", default=0
    )

    # AUD-4: per-group 18+ RP gate (FEAT-RP). These ALREADY exist in prod
    # from legacy (``bot.py:5854-5861`` add them via ``ALTER TABLE
    # group_settings ADD COLUMN`` at startup); migration
    # ``0006_rp_18_gate`` adds them to fresh/test DBs only. ``rp_18_enabled``
    # is the master per-group toggle (legacy default OFF — 18+ RP refused
    # until a group admin enables it); ``rp_18_prompt_sent`` tracks the
    # one-time "an admin enabled this?" affordance so the inline prompt is
    # shown at most once per group (legacy ``bot.py:21770-21774``). Integer
    # 0/1 mirrors the legacy ALTER defaults so a row built by either side
    # reads identically. The legacy ``rp_18_confirmed_at`` TEXT column is
    # NOT modelled — the new pipeline never reads it (additive-only policy,
    # see the class docstring).
    rp_18_enabled: Mapped[int] = mapped_column(
        Integer, nullable=True, server_default="0", default=0
    )
    rp_18_prompt_sent: Mapped[int] = mapped_column(
        Integer, nullable=True, server_default="0", default=0
    )

    # #270: per-group "VIP may RP outside a relationship" toggle. Same
    # additive-only story as the two above — it ALREADY exists in prod
    # from legacy (``bot.py:5858-5859`` adds it via ``ALTER TABLE
    # group_settings ADD COLUMN rp_vip_outside_enabled INTEGER DEFAULT 1``
    # at startup, and a live PRAGMA confirms column 16 with default 1);
    # migration ``0010_rp_vip_outside`` adds it to fresh/test DBs only.
    # Note the default is ON, unlike the 18+ gate: legacy sold the perk
    # switched on and let a group admin turn it off (``bot.py:7798``,
    # ``bot.py:7827``), so a group that has never touched the setting
    # must read as enabled. Modelling it with ``server_default="0"``
    # would silently revoke a paid feature for every existing group.
    rp_vip_outside_enabled: Mapped[int] = mapped_column(
        Integer, nullable=True, server_default="1", default=1
    )


class VoiceTranscription(UsersBase):
    """One transcribed group voice message (L-70).

    Schema mirrors the legacy ``voice_transcriptions`` table EXACTLY
    (``bot.py:5899-5917`` ``CREATE TABLE IF NOT EXISTS``), which already
    exists in prod in ``users.db``. The new OpenAI-Whisper pipeline owns
    writes from this stage on (``handlers/voice_transcribe``); legacy
    wrote the same table until T-011 removed it, and both used an
    AUTOINCREMENT PK, so the table holds an unbroken sequence written by
    two writers rather than two ranges that need reconciling.

    ``created_at`` is filled Python-side from :func:`db_now` (naive-UTC)
    so the new writer matches the rest of the new pipeline's timestamp
    convention; legacy rows use SQLite's ``CURRENT_TIMESTAMP`` server
    default (also naive-UTC text), so both sides remain comparable. The
    column keeps the server default too for any insert that bypasses the
    ORM.
    """

    __tablename__ = "voice_transcriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_unique_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcribed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=db_now)

    __table_args__ = (
        Index("idx_voice_transcriptions_group", "group_id"),
        # #1938: the per-speaker daily budget filters on ``user_id``
        # alone, across every group — the group index cannot serve it.
        Index("idx_voice_transcriptions_user", "user_id"),
    )


class BotGroup(UsersBase):
    """One row per chat the bot has been added to.

    Written by the new pipeline since #111: the ``my_chat_member``
    handlers register the group on join and clear ``is_active`` on
    leave. ``/admin_botstats`` counts rows here for the "groups" tally
    — same query legacy uses, just through SQLAlchemy instead of raw
    ``sqlite3``. Schema mirrors ``docs/prod_schemas.sql`` so
    ``Base.metadata.create_all`` in tests produces a table legacy could
    still write to without ALTER.

    ``is_active`` is a flag rather than a delete because the row carries
    the payout attribution: keeping it means a re-add restores the owner
    the group already had instead of naming whoever re-added the bot.
    """

    __tablename__ = "bot_groups"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    added_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    chat_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_has_admin_rights: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (Index("idx_bot_groups_added_by", "added_by_user_id"),)


class UserGroupNickname(UsersBase):
    """Per-chat display-name override (legacy: ``user_group_nicknames``).

    Stage 33 owns reads AND writes for this table — it's a tiny
    self-contained feature whose entire data model is "(user, chat) →
    display name". Legacy wrote it via ``set_user_group_nickname`` (a raw
    ``sqlite3`` upsert at ``bot.py:6925``); since the cutover the only
    writer is ``NicknamesRepo`` through the SQLAlchemy mapper, so the
    two-writers-one-file question this paragraph used to answer no
    longer arises.

    Schema mirrors ``docs/prod_schemas.sql`` exactly so
    :meth:`Base.metadata.create_all` in tests builds the same table the
    prod rows live in, no ALTER on either side. ``updated_at`` is
    server-side (``func.now()`` here, ``datetime('now')`` in the rows
    legacy left) so the stored time never depends on an application
    clock.
    """

    __tablename__ = "user_group_nicknames"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_user_group_nicknames_chat", "chat_id"),)


class Marriage(UsersBase):
    """One marriage pair, scoped to a single chat (legacy schema).

    Stage 19 only reads from this table (group leaderboard via
    ``/marriages``); writes live in legacy. Every prod column is
    declared so :meth:`Base.metadata.create_all` in tests produces
    a schema-compatible table — a missing column would let legacy
    write a row the new ORM couldn't read, surfacing as silent NULLs
    in the leaderboard.

    Source: ``docs/prod_schemas.sql``.
    """

    __tablename__ = "marriages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user1_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user2_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    experience: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ``NULL`` and ``'active'`` BOTH mean active in legacy (see the
    # WHERE clause at bot.py:22991). The new repo encodes the same.
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="active")
    divorced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restore_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    in_top: Mapped[int] = mapped_column(Integer, nullable=True, default=1)
    """Whether the pair appears on ``/marriages`` (#2021).

    ``1`` is on the board, which is why the Python-side default is 1 and
    prod's column DEFAULT is still the 0 legacy shipped: legacy wrote
    the flag from ``/marry_top_on``/``/marry_top_off`` and never read it
    back, so every existing row holds 0 while being on the board.
    Revision ``0012_marriages_in_top_backfill`` reconciles the two by
    lifting those rows to 1; the column default is deliberately NOT
    rebuilt, because the only writer that would inherit it is the legacy
    monolith, which no longer runs. Reads coalesce NULL to 1 — an
    unmigrated row is one nobody ever expressed a preference about.
    """
    auto_divorce: Mapped[str] = mapped_column(String, nullable=True, default="off")
    duration_days: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    last_extended: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Prod carries UNIQUE(chat_id, user1_id, user2_id) here
    # (``docs/prod_schemas.sql:79``). Declaring it is not cosmetic: test
    # DBs are built by ``metadata.create_all``, not by migrations, so
    # without this line a duplicate insert that prod REJECTS succeeds
    # in tests — the one class of bug the schema mirror exists to catch.
    # The absence of a FOREIGN KEY on ``user1_id``/``user2_id`` is
    # deliberate prod parity, not an oversight: prod has none either
    # (``docs/prod_schemas.sql:73-80``), and adding one here would make
    # tests reject rows production accepts — the same drift in reverse.
    __table_args__ = (
        Index("idx_marriages_chat", "chat_id"),
        UniqueConstraint("chat_id", "user1_id", "user2_id"),
    )


class Relationship(UsersBase):
    """One relationship pair, scoped to a single chat.

    Same shape as :class:`Marriage` minus the marriage-specific
    duration/auto-divorce/restore columns; legacy stores the two
    relationship tiers in separate tables (different XP curves and
    different status transitions). Stage 19 shipped the read side; the
    writes followed it. ``BondsRepo`` decays an inactive pair's XP on
    read, adds XP through ``add_relationship_xp``, and ends every bond
    a leaver holds through ``end_relationships_for``.
    """

    __tablename__ = "relationships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user1_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user2_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    experience: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="active")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Same as :class:`Marriage` above: prod has the UNIQUE
    # (``docs/prod_schemas.sql:96``) and deliberately no FOREIGN KEY
    # (``:90-97``).
    __table_args__ = (
        Index("idx_relationships_chat", "chat_id"),
        UniqueConstraint("chat_id", "user1_id", "user2_id"),
    )


class MarriageProposal(UsersBase):
    """Pending marriage proposal (``marriage_proposals`` table).

    Legacy schema (bot.py:5535): one row per outstanding proposal;
    accepted/declined rows are deleted immediately by the handler.
    The new pipeline owns writes here from Stage T-019 onward; legacy
    wrote the same table until T-011 removed it, and both sides used an
    AUTOINCREMENT PK, so nothing has to be reconciled — but a proposal
    legacy left behind is still a row here, which is what the ``status``
    default below has to stay safe against.
    """

    __tablename__ = "marriage_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    from_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Atomic claim flag for the accept/decline race (R-FIX-010). The
    # repo flips this from 'pending' to 'accepted'/'declined' in a single
    # UPDATE...WHERE status='pending' RETURNING id; only the winning
    # caller proceeds to create the marriage row. Legacy bot.py never
    # touched this column — it deleted proposals on resolve — so a row
    # it left behind reads as 'pending' under the server default, which
    # is exactly what an outstanding legacy proposal was.
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending", default="pending"
    )

    __table_args__ = (Index("idx_marriage_proposals_to", "chat_id", "to_id"),)


class RelationshipProposal(UsersBase):
    """Pending relationship proposal (``relationship_proposals`` table).

    Legacy schema (bot.py:5557): one row per outstanding proposal —
    ``id, chat_id, from_id, to_id, created_at`` plus an index on
    ``(chat_id, to_id)``. Accepted/declined rows are deleted by the
    handler. The ``status`` column is **new** (added by migration
    ``0003_relationship_proposal_status``) so the accept/decline flow
    can claim a row atomically, mirroring the marriage-proposal
    double-accept fix (R-FIX-010). Legacy never wrote this column —
    it DELETEd the row on resolve — so a proposal it left behind reads
    as ``'pending'`` under the server default, which is exactly what an
    outstanding legacy proposal was. Same reasoning as
    :class:`MarriageProposal`.
    """

    __tablename__ = "relationship_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    from_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending", default="pending"
    )

    __table_args__ = (Index("idx_relationship_proposals_to", "chat_id", "to_id"),)
