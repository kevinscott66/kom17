"""Typed application settings (pydantic-settings v2).

Single source of truth for the whole application. Reads from environment
variables (loaded from ``.env`` by ``pydantic-settings`` automatically).

The migration is over, and with it the split this docstring used to
describe: the legacy monolith parsed its own environment, and this
module now parses all of it — transport, payments, AI and speech,
economy, moderation, observability, paths. ``.env.example`` is checked
against these defaults by a regression test, so the two cannot drift.

Nothing reads a secret at import time: secrets stay ``SecretStr`` until
the call site asks for the value.
"""

from __future__ import annotations

import logging
import os
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from telegram_invite_bot.services.payments.rates import COINS_PER_USD

# M-I-2: prefixes the project owns that CANNOT be derived from the
# Settings tree, because no field carries them. Everything else is
# derived at runtime by :func:`_known_env_prefixes` — this seed exists
# only for the leftovers:
#
# * ``ENABLE_NEW_`` / ``OPENWEATHER_`` name features that were removed.
#   An operator still setting them is misconfigured and should hear
#   about it, so the prefixes outlive their fields on purpose.
# * ``GUIDES_`` backs ``GUIDES_EDIT_SECRET``, which the CMS editor reads
#   straight from ``os.environ``
#   (:func:`cms.guide_site.editor._read_edit_secret`) rather than
#   through Settings.
#
# Every entry ends in an underscore, and that is a rule rather than a
# coincidence — see :func:`_derive_env_prefix`, which always appends
# one. A prefix that stops mid-word matches other people's variables:
# the retired switch used to be seeded as the bare verb ``ENABLE_``,
# which made the bot announce a GitHub runner's own
# ``ENABLE_RUNNER_TRACING`` as "likely a typo" of ours, and the bare
# ``HOST`` / ``PORT`` seeds did the same to the ``HOSTNAME`` that every
# container runtime exports. Accusing a neighbour's variable of being
# our typo is worse than staying quiet: it trains the operator to read
# past the one warning that exists to be read.
#
# This list used to be the WHOLE prefix set and had drifted 24 real
# operator-settable variables behind the Settings tree (COINS_*,
# MESSAGE_REWARD_*, LEGAL_*, SUPPORT_*, PVP_*, AI_QUOTA_* and more),
# which is exactly the class of typo the check exists to catch.
_SEED_ENV_PREFIXES: tuple[str, ...] = (
    "ENABLE_NEW_",
    "GUIDES_",
    "OPENWEATHER_",
)

#: Env vars the project reads outside Settings. Without this the stray
#: check would flag a CORRECTLY spelled ``GUIDES_EDIT_SECRET``, since it
#: matches a seed prefix but is not a Settings alias.
_NON_SETTINGS_ENV_KEYS: frozenset[str] = frozenset({"GUIDES_EDIT_SECRET"})


class AppEnv(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class LogLevel(StrEnum):
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class BotConfig(BaseSettings):
    """Telegram bot identity. Shared with legacy ``bot.py`` (same token)."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    token: SecretStr = Field(..., alias="BOT_TOKEN")

    # admin/owner identifiers — used by middlewares/handlers as they migrate.
    admin_chat_id: int = Field(default=0, alias="ADMIN_CHAT_ID")
    main_chat_id: int = Field(default=0, alias="CHAT_ID")
    log_bot_chat_id: int | None = Field(default=None, alias="LOG_BOT_CHAT_ID")

    # Developer (bot-owner) Telegram IDs. Legacy read up to four
    # numbered env vars (``DEVELOPER_ID_1`` … ``DEVELOPER_ID_4``) and
    # fell back to ``ADMIN_CHAT_ID`` when none were set. That loader is
    # mirrored EXACTLY. The original reason — keeping two live layers on
    # one owner set — died with the legacy process in T-011; the reason
    # that outlived it is the ``.env`` on the prod host, which is the
    # same file legacy was configured from. Changing the shape here
    # would silently re-read an operator's existing file as a different
    # owner set, and "is this user a developer" gates the admin cards.
    developer_id_1: int | None = Field(default=None, alias="DEVELOPER_ID_1")
    developer_id_2: int | None = Field(default=None, alias="DEVELOPER_ID_2")
    developer_id_3: int | None = Field(default=None, alias="DEVELOPER_ID_3")
    developer_id_4: int | None = Field(default=None, alias="DEVELOPER_ID_4")

    # R-FIX-011-fp: allow moderation by anonymous admins (Telegram routes
    # "Remain anonymous" actions through GroupAnonymousBot id=1087968824
    # OR sets ``sender_chat`` to the chat itself when an owner posts
    # "as the group/channel"). Legacy allowed this — refusing every
    # anonymous owner was an over-eager lockdown in iter-1 because the
    # owner-with-Remain-anonymous-on posture is the default for many
    # large groups. The new gate verifies via ``getChatAdministrators``
    # that at least one admin exists in the chat (the actor IS provably
    # an admin, just not attributable to a specific human id). Audit
    # rows record ``actor_id=sender_chat.id`` with an ``anonymous=True``
    # marker. Operators who want the locked-down iter-1 posture can set
    # ``ALLOW_ANONYMOUS_ADMIN=false`` in the environment.
    allow_anonymous_admin: bool = Field(default=True, alias="ALLOW_ANONYMOUS_ADMIN")

    # L-61: per-user cooldown between /ad advertiser requests (legacy
    # AD_REQUEST_COOLDOWN 24h, bot.py:2797).
    ads_request_cooldown_hours: int = Field(default=24, alias="ADS_REQUEST_COOLDOWN_HOURS", ge=0)

    # EPIC ranks (R3, approved deviation): legacy staff-sync silently
    # auto-promoted every TG admin to rank 2; OFF by default here —
    # only demote-on-loss applies. Flip via RANK_AUTOSYNC=1.
    rank_autosync: bool = Field(default=False, alias="RANK_AUTOSYNC")

    @property
    def developer_ids(self) -> frozenset[int]:
        """Resolved set of developer user IDs.

        Built from ``DEVELOPER_ID_1..4`` (legacy numbered form — chose
        compatibility over a single comma-separated list because the
        deployed prod ``.env`` already uses the numbered names
        and we MUST NOT require an env edit just to deploy the new
        pipeline). Falls back to ``ADMIN_CHAT_ID`` if none of the
        numbered ones are set, same as ``bot.py:_load_developer_ids``.

        ``frozenset`` (not list) so membership checks are O(1) and the
        object is hashable + immutable — handlers receive it through
        DI and shouldn't be able to mutate the owner set at runtime.
        Non-positive IDs are dropped: Telegram user IDs are always
        positive, and a typo'd ``0`` in env would otherwise grant
        developer rights to whatever update happens to lack a from-user.
        """
        numbered = (
            self.developer_id_1,
            self.developer_id_2,
            self.developer_id_3,
            self.developer_id_4,
        )
        ids = {uid for uid in numbered if uid is not None and uid > 0}
        if not ids and self.admin_chat_id > 0:
            ids = {self.admin_chat_id}
        return frozenset(ids)

    def is_developer(self, user_id: int) -> bool:
        """``True`` iff ``user_id`` is a recognised bot owner.

        Lifted out of the per-handler ``if uid in DEVELOPER_IDS`` checks
        scattered across legacy ``bot.py`` so future handlers can call
        one named method instead of reaching into the raw frozenset
        (which makes the "what does this branch check?" grep easier).
        """
        return user_id in self.developer_ids


#: Telegram Bot API restricts ``secret_token`` to 1-256 characters of
#: ``A-Za-z0-9_-``. Anything else is rejected by ``setWebhook`` itself,
#: so accepting it here only defers the failure to a place where it is
#: invisible — see ``Settings._webhook_secret_token_charset``, which is on
#: the outer model rather than on ``WebhookConfig`` so that a rejection
#: cannot make pydantic echo the secret into the error text.
_SECRET_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


class WebhookConfig(BaseSettings):
    # NOTE: ``populate_by_name`` is deliberately False here. The field
    # name ``path`` would otherwise collide with the shell ``$PATH``
    # env var (pydantic-settings does a case-insensitive lookup of the
    # field name when ``populate_by_name=True``, and the shell's
    # ``PATH`` is set by every login process). This was a real bug
    # surfaced by the test suite: ``s.webhook.path`` ended up
    # containing the operator's ``$PATH`` instead of ``/webhook``.
    # Aliases are unambiguous so only-aliases is enough to populate
    # every field correctly.
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=False,
    )

    url: str = Field(default="", alias="WEBHOOK_URL")
    path: str = Field(default="/webhook", alias="WEBHOOK_PATH")
    # Loopback by default, because a default is what an operator gets
    # when they have not thought about it. Every deployment we support
    # puts a reverse proxy in front — nginx terminates TLS and proxies
    # to 127.0.0.1:8080 — so bind-all buys nothing there and costs a
    # great deal on a host without a firewall: ``/metrics`` and
    # ``/readyz`` are protected by nginx prefix ACLs, and a socket bound
    # to every interface is a way around nginx entirely. The one place
    # bind-all *is* correct is inside a container, where the container
    # boundary is the firewall and loopback would make the published
    # port unreachable; the Dockerfile therefore sets ``HOST=0.0.0.0``
    # explicitly. An operator who means it says so the same way.
    host: str = Field(default="127.0.0.1", alias="HOST")
    # #1933: bounded on purpose. ``PORT=0`` is legal for ``bind()`` and
    # means "pick any free ephemeral port" — the server comes up, the
    # process stays alive, ``/healthz`` answers on a port nobody knows,
    # and nginx proxies to 8080 into the void. That is exactly the
    # alive-but-not-serving posture #1929 exists to prevent, except it
    # slips past every liveness check we have. Negative and >65535
    # values fail later inside the socket layer with an opaque message;
    # rejecting them at Settings load names the offending env var.
    port: int = Field(default=8080, alias="PORT", ge=1, le=65535)

    secret_token: SecretStr | None = Field(default=None, alias="WEBHOOK_SECRET_TOKEN")

    @field_validator("secret_token", mode="after")
    @classmethod
    def _empty_secret_token_is_none(cls, v: SecretStr | None) -> SecretStr | None:
        """Treat an empty / whitespace ``WEBHOOK_SECRET_TOKEN`` as unset.

        SEC audit: an operator who sets the env var to ``""`` would
        otherwise get a non-``None`` :class:`SecretStr` whose value is the
        empty string. ``verify_secret_token`` would then compare the
        incoming header against ``""`` — and a request with NO header
        (``provided == ""``) compares equal, so anyone could forge updates
        while the config *looks* secured. Normalising empty → ``None``
        routes it back through the single "no secret" path: a no-op in
        dev, and a hard startup failure in prod (the
        ``_require_secret_token_in_prod`` validator refuses to boot
        without a real secret) instead of a silent auth bypass.
        """
        if v is not None and not v.get_secret_value().strip():
            return None
        return v

    # #2023 follow-up: the deliberate, *declared* way to run a webhook
    # with no secret. Registering a public URL unauthenticated used to
    # be the default state and merely logged an error — an operator who
    # never read the boot log could not tell a secured deployment from
    # an open one. Startup now refuses instead, and this flag is how a
    # developer says "yes, I know, it is a tunnel to my laptop". The
    # point is not to make the insecure mode unreachable; it is to make
    # it impossible to be in it by accident.
    #
    # It cannot weaken production: ``_require_secret_token_in_prod``
    # runs first and refuses ``APP_ENV=prod`` without a real secret
    # whatever this is set to.
    allow_insecure: bool = Field(default=False, alias="ALLOW_INSECURE_WEBHOOK")

    ssl_cert: Path | None = Field(default=None, alias="SSL_CERT")
    ssl_key: Path | None = Field(default=None, alias="SSL_KEY")

    # M-I-8: comma-separated list of trusted upstream proxy IPs whose
    # ``X-Forwarded-For`` / ``X-Forwarded-Proto`` headers uvicorn will
    # honour. Default ``127.0.0.1`` matches the nginx-front prod
    # topology — nginx terminates TLS on the same host and forwards
    # to uvicorn on loopback. A wider default (``*``) would let any
    # client spoof their IP by sending the header themselves, since
    # uvicorn doesn't ship with proxy_headers enabled by default.
    forwarded_allow_ips: str = Field(default="127.0.0.1", alias="FORWARDED_ALLOW_IPS")

    @field_validator("path")
    @classmethod
    def _path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("WEBHOOK_PATH must start with '/'")
        return value

    @model_validator(mode="after")
    def _ssl_pair_must_match(self) -> WebhookConfig:
        """``SSL_CERT`` and ``SSL_KEY`` are both-or-neither.

        ``runner/webhook.py`` passes them to ``uvicorn.Config`` and uvicorn
        only enables HTTPS when *both* are present — supplying just one
        used to be a silent footgun (uvicorn falls back to HTTP and the
        operator only finds out when Telegram refuses to call the webhook).
        Catch the asymmetry at startup with a precise message instead.
        """
        if (self.ssl_cert is None) != (self.ssl_key is None):
            missing = "SSL_KEY" if self.ssl_cert else "SSL_CERT"
            raise ValueError(
                f"{missing} is required when the other half of the SSL pair is set "
                "(uvicorn silently falls back to HTTP otherwise)"
            )
        return self


class PathsConfig(BaseSettings):
    """Filesystem locations. Matches legacy resolution from ``bot.py:737-743``."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    database_dir: Path = Field(default=Path("database"), alias="DATABASE_DIR")
    message_stats_dir: Path | None = Field(default=None, alias="MESSAGE_STATS_DIR")
    settings_file: Path | None = Field(default=None, alias="SETTINGS_FILE")
    logs_dir: Path = Field(default=Path("logs"), alias="LOGS_DIR")

    def resolved_message_stats_dir(self) -> Path:
        # NOTE: paths are not .resolve()'d here — relative values from .env stay
        # relative (matches legacy behaviour). Stage 2 normalises against the
        # process CWD once the DB layer needs absolute paths.
        return self.message_stats_dir or self.database_dir

    def resolved_settings_file(self) -> Path:
        return self.settings_file or (self.database_dir / "settings.json")


class LoggingConfig(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    level: LogLevel = Field(default=LogLevel.INFO, alias="LOG_LEVEL")
    json_format: bool = Field(default=False, alias="LOG_JSON")


class ObservabilityConfig(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    sentry_dsn: SecretStr | None = Field(default=None, alias="SENTRY_DSN")
    # #1995. This field exists because a comment in
    # ``observability.sentry`` already told operators to "raise via
    # SENTRY_TRACES_SAMPLE_RATE in incidents" while the rate was
    # hardcoded to 0.0 — an instruction that failed at exactly the
    # moment it was written for. Default stays 0.0, so a deployment
    # that sets nothing behaves as before; the bounds are Sentry's
    # own (a rate outside [0, 1] is silently meaningless to the SDK,
    # so we refuse it at load instead).
    sentry_traces_sample_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, alias="SENTRY_TRACES_SAMPLE_RATE"
    )


class HelpConfig(BaseSettings):
    """External links rendered by ``/help``.

    Legacy reads the same URLs from ``settings.json`` (keys
    ``telegraph_commands_url`` / ``..._en``). The new pipeline takes them
    from the environment so that pydantic-settings stays the single
    source of truth; deploy wires both via ``.env`` until the
    ``settings.json`` → typed-config migration (plan Stage 14).
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    telegraph_commands_url_ru: str | None = Field(default=None, alias="TELEGRAPH_COMMANDS_URL")
    telegraph_commands_url_en: str | None = Field(default=None, alias="TELEGRAPH_COMMANDS_URL_EN")

    def guide_url(self, lang: str) -> str | None:
        """The command-guide URL for ``lang``, or ``None`` if unusable.

        Both ``/help`` and ``/faq`` render this as an inline URL button
        (RR-6 #70), so the resolution lives here rather than being
        copy-pasted into two handlers with subtly different fallbacks.
        Non-``en`` resolves to the Russian link, matching how
        :func:`~telegram_invite_bot.i18n.t` falls back for any language
        the bot doesn't ship — the button must not point at a guide in
        a language the surrounding card isn't written in.

        The scheme check is deliberate: these values come from the
        deployment's ``.env`` unvalidated, and Telegram rejects the
        whole ``sendMessage`` when a URL button carries a scheme it
        doesn't accept. Without the guard an operator typo would not
        degrade the button — it would take down ``/help`` and ``/faq``
        entirely.
        """
        url = self.telegraph_commands_url_ru if lang != "en" else self.telegraph_commands_url_en
        if not url or not url.startswith(("http://", "https://")):
            return None
        return url


class AiConfig(BaseSettings):
    """DeepSeek chat-completion knobs for ``/ask``.

    Field names match legacy's env-var keys (``DEEPSEEK_*``) so the
    same ``.env`` keeps working when the new path goes live.

    #1657: there is no "modular legacy service" to have taken these
    defaults from. The docstring used to name
    ``bot/services/ai_service.py``, a directory that has never existed
    here — legacy is one file at the root. The real source is the
    monolith's own settings dict (``bot.py:2758``), and the match is
    close but not exact: ``model`` (``deepseek-chat``) and
    ``temperature`` (0.7) are identical, and #1661 brought ``max_tokens``
    back to legacy's 1000 as well. 1024 read as a round-to-a-power-of-two
    slip during the port rather than a decision — the two neighbouring
    fields were carried across verbatim — and this one is the per-answer
    ceiling on a paid API with no ``DEEPSEEK_MAX_TOKENS`` set on prod, so
    the default here is what actually bills the operator. Raising it is
    an env-var away for anyone who wants longer answers. What genuinely
    was left behind is the
    monolith's mode-aware variant — per-mode system prompts, weather
    injection, group context — which lands in a later stage once those
    have homes in this codebase.

    ``api_key`` is optional at the config level so the handler can
    surface a friendly "not configured" message instead of crashing
    on startup when an operator hasn't set the key yet.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    api_key: SecretStr | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    api_url: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        alias="DEEPSEEK_API_URL",
    )
    timeout_seconds: float = Field(default=60.0, alias="DEEPSEEK_TIMEOUT_SECONDS", ge=1.0, le=300.0)
    max_tokens: int = Field(default=1000, alias="DEEPSEEK_MAX_TOKENS", ge=1, le=8192)
    temperature: float = Field(default=0.7, alias="DEEPSEEK_TEMPERATURE", ge=0.0, le=2.0)


class CurrencyConfig(BaseSettings):
    """Upstream FX source for the COM→currency converter (``/rate``, ``/convert``).

    Optional ``api_key`` selects the **keyed v6** exchangerate-api.com
    endpoint (payload key ``conversion_rates``); when it is unset the
    service falls back to the **keyless v4** endpoint (payload key
    ``rates``). Either way ``COM``/``RUB`` are pinned by design and crypto
    rates come from the hardcoded fallback table — the upstream only
    supplies the derivable fiat rates. The key is a secret, so it is read
    from the environment (``.env``) and never committed.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    api_key: SecretStr | None = Field(default=None, alias="EXCHANGERATE_API_KEY")
    timeout_seconds: float = Field(default=10.0, alias="CURRENCY_TIMEOUT_SECONDS", ge=1.0, le=60.0)
    cache_ttl_seconds: float = Field(default=3600.0, alias="CURRENCY_CACHE_TTL_SECONDS", ge=0.0)


class OpenAiConfig(BaseSettings):
    """OpenAI chat-completion knobs for ``/ai``, ``/gpt``, ``/chat`` (T-021).

    Separate from :class:`AiConfig` (DeepSeek for ``/ask``) because the
    two ports speak different APIs and have different billing posture:
    ``/ask`` is unmetered today; ``/ai`` / ``/gpt`` / ``/chat`` debit
    the user's wallet via :class:`EconomyService` after the response
    lands (1 token = 0.001 coin, rounded up). Sharing one config class
    would force every operator to think about which knobs apply to
    which path; two classes keep the env-var namespace flat and the
    DI wiring explicit.

    ``api_key`` is optional so the handler can degrade gracefully to
    a "не настроен" i18n message rather than crashing on startup when
    the operator hasn't set ``OPENAI_API_KEY`` yet. T-023 (vip emoji
    voice TTS) will share the same key — adding a second subconfig
    later would force two ``OPENAI_API_KEY`` env vars, which is
    operator-unfriendly.

    Historical note: ``coin_per_token`` and ``max_tokens`` originally
    powered an OpenAI-billed ``/ai``/``/gpt``/``/chat`` path that the
    DeepSeek realignment retired. They survive as no-op knobs (TTS
    uses ``coin_per_char``, DeepSeek charges nothing) so existing
    ``.env`` files don't break on startup — pydantic-settings ignores
    extras anyway. Safe to drop in a future cleanup pass.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    max_tokens: int = Field(default=512, alias="OPENAI_MAX_TOKENS", ge=1, le=8192)
    temperature: float = Field(default=0.7, alias="OPENAI_TEMPERATURE", ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=30.0, alias="OPENAI_TIMEOUT_SECONDS", ge=1.0, le=300.0)
    # 1 token = 0.001 coin. Stored as float so a future operator
    # adjustment (e.g. 0.0005) doesn't require a code change. The
    # service rounds the final debit UP to the nearest integer coin
    # so we never under-charge — the user effectively pays a fraction
    # of a coin extra on small calls (sub-cent rounding), which is
    # acceptable against the alternative of silently absorbing the
    # cost difference into ops.
    coin_per_token: float = Field(default=0.001, alias="OPENAI_COIN_PER_TOKEN", gt=0.0, le=1.0)
    # Group STT (``handlers/voice_transcribe``) is the one OpenAI-billed
    # path with NO user-side price: the group admin flips it on and every
    # voice note in that chat becomes a paid Whisper call charged to the
    # operator's key. ``/ai`` / ``/ask`` / ``/voice`` are gated by
    # ``AiQuotaService``; this passive path had nothing, so a single busy
    # (or hostile) group could run the key's budget down unattended.
    #
    # Per-group, per-UTC-day ceiling. ``0`` means unlimited — the same
    # "0 == no cap" convention ``AiQuotaConfig`` uses for VIP. The default
    # is deliberately generous: a real group rarely posts 200 voice notes
    # a day, so the cap only ever bites abuse.
    stt_group_daily_limit: int = Field(default=200, alias="OPENAI_STT_GROUP_DAILY_LIMIT", ge=0)

    # The call-count ceiling above bounds the wrong quantity on its own.
    # Whisper bills per second of audio, not per request, and Telegram
    # will hand a bot a voice note of any length up to the 20MB
    # ``getFile`` limit — which for ogg/opus is hours, not minutes. Two
    # hundred long voices is therefore two hundred times more expensive
    # than the two hundred short ones the ceiling was sized against.
    #
    # ``stt_max_voice_seconds`` refuses a single overlong voice outright;
    # ``stt_group_daily_seconds`` is the ceiling that actually bounds the
    # bill, because it accumulates. Both are free to check: Telegram
    # sends the duration in the update itself, before any download.
    #
    # The defaults are sized off ordinary use, not off abuse. A spoken
    # message runs ten to thirty seconds, so an hour of audio a day is a
    # busy group's worth of chatter and roughly the same number of
    # messages the call ceiling already allows — while capping a single
    # group at cents a day instead of dollars. ``0`` means unlimited on
    # both, the same convention the call ceiling uses.
    stt_max_voice_seconds: int = Field(default=300, alias="OPENAI_STT_MAX_VOICE_SECONDS", ge=0)
    stt_group_daily_seconds: int = Field(default=3600, alias="OPENAI_STT_GROUP_DAILY_SECONDS", ge=0)

    # #1938: both ceilings above are denominated PER GROUP, and a group
    # costs nothing to make. Anyone can create a chat, add the bot, flip
    # transcription on from ``/voice_settings`` (the only requirement is
    # being an admin there, which the creator is by definition — there
    # is no allow-list of groups in this port, see
    # ``handlers/group_events.py:262-273``), and spend a fresh 3600-second
    # allowance. Fifty such chats is fifty hours of billed audio a day
    # with every per-group counter comfortably inside its budget and
    # not one warning logged, because a warning only fires when a cap
    # is REACHED. The comment above reasons about "one busy (or
    # hostile) group" and closes that case; multiplying groups walks
    # straight around it.
    #
    # This ceiling is keyed on the SPEAKER and spans every chat, so the
    # same person opening more groups buys nothing. It is deliberately
    # denominated in seconds rather than calls: seconds are what the
    # invoice is computed from, the same argument
    # ``stt_group_daily_seconds`` rests on.
    #
    # Half an hour of audio a day from ONE person is far beyond
    # ordinary use — a spoken message runs ten to thirty seconds, so
    # this is sixty to a hundred and eighty voice notes — while the
    # abuse shape it stops is unbounded. ``0`` means unlimited, the
    # same convention as its siblings.
    #
    # Deliberately NOT added here: a global daily budget across all
    # users. It would be the only knob in this file whose activation
    # silently disables a working feature for everybody, i.e. an
    # operator-visible kill switch, and that is a policy decision for
    # the owner rather than a hole to be quietly plugged.
    stt_user_daily_seconds: int = Field(default=1800, alias="OPENAI_STT_USER_DAILY_SECONDS", ge=0)


class TtsConfig(BaseSettings):
    """OpenAI TTS knobs for ``vip_emoji_voice`` (T-023).

    Shares :attr:`OpenAiConfig.api_key` — there is intentionally NO
    second ``api_key`` field here. The handler receives both
    :class:`OpenAiConfig` and :class:`TtsConfig` and passes the key
    through to :class:`TtsService`, so a single ``OPENAI_API_KEY`` env
    var powers both T-021 chat-completions and T-023 TTS. A duplicate
    key field would force operators to set the same value twice, with
    the obvious drift hazard the moment one of the two is rotated.

    Coin-budget formula (analog of T-021's per-token cost):
        coin_charge = max(1, ceil(len(text) * coin_per_char))

    Defaults match ADR 0013: ``nova`` voice (warm, neutral RU+EN
    pronunciation), ``tts-1`` model (the cheap tier — ``tts-1-hd`` is
    2x cost for marginal quality on a Telegram voice note), and
    ``0.001 coin/char`` — at 1000 chars (one paragraph) that's 1
    coin, mirroring T-021's per-token order of magnitude. OpenAI's
    published price is $0.015 per 1k chars for tts-1; at the bot's
    coin-to-fiat peg this lands solidly above cost-recovery without
    being punitive.

    ``timeout_seconds`` is higher than T-021's 30s because TTS
    payloads (audio bytes) take longer to ship than text tokens — a
    2000-char paragraph easily produces 100KB of MP3 even on a fast
    link.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    voice: str = Field(default="nova", alias="OPENAI_TTS_VOICE")
    model: str = Field(default="tts-1", alias="OPENAI_TTS_MODEL")
    # 1 char = 0.001 coin (analog of OPENAI_COIN_PER_TOKEN). Stored
    # as float so an operator can drop to 0.0005 without a code
    # change. The service rounds the final debit UP — symmetric to
    # T-021's rounding posture.
    coin_per_char: float = Field(default=0.001, alias="OPENAI_TTS_COIN_PER_CHAR", gt=0.0, le=1.0)
    timeout_seconds: float = Field(
        default=60.0, alias="OPENAI_TTS_TIMEOUT_SECONDS", ge=1.0, le=300.0
    )
    # Hard cap on the user-supplied text length. OpenAI's TTS API
    # accepts up to 4096 chars per request; we mirror that ceiling
    # so the handler can refuse over-long inputs early instead of
    # paying the upstream round-trip just to receive a 400.
    max_chars: int = Field(default=4096, alias="OPENAI_TTS_MAX_CHARS", ge=1, le=4096)
    # M-P-3: hard cap on the upstream audio payload before we try to
    # upload it to Telegram. Defaults to Telegram's voice-message
    # ceiling (50MB documented; we set 25MB to leave headroom for the
    # multipart-form overhead and to keep webhook timeouts bounded).
    # A payload above the cap → AUDIO_TOO_LARGE; the pre-authorised
    # debit is refunded so the user is not billed for audio they
    # never receive.
    max_audio_bytes: int = Field(
        default=25 * 1024 * 1024,
        alias="OPENAI_TTS_MAX_AUDIO_BYTES",
        ge=1024,
        le=50 * 1024 * 1024,
    )
    # #1963: the per-VIP daily synthesis ceiling enforced by
    # ``VoiceQuotaService``. It lived as a bare default on
    # ``VoiceQuotaConfig`` and the one construction site never passed
    # it, so the number its own module calls "Operator-tunable" was
    # reachable only by editing source — while the AI quota next door
    # has been env-driven since it was written. TTS is the metered one
    # of the two, so this is the ceiling an operator most needs during
    # an upstream cost incident; the only lever that existed was the
    # unlimited allowlist, which REMOVES the cap rather than lowering
    # it. ``0`` means unlimited, the same convention as
    # ``AI_QUOTA_VIP_DAILY_LIMIT`` and the STT budgets above.
    vip_daily_limit: int = Field(default=20, alias="OPENAI_TTS_VIP_DAILY_LIMIT", ge=0, le=10_000)


class PaymentsConfig(BaseSettings):
    """Provider secrets for the payment webhooks (T-025).

    Four providers live behind one config class because they share a
    single conceptual surface (server-to-server payment confirmation)
    and an operator usually configures all of them or none. Splitting
    into ``CryptoConfig`` / ``YooKassaConfig`` / ``StripeConfig`` /
    ``RollyPayConfig`` would multiply the import noise at the router
    callsite for zero ops gain — the env-var names are already
    provider-scoped.

    Every secret is ``Optional[SecretStr]`` (or ``str | None`` for the
    YooKassa shop id, which is a public account identifier — but it
    still gates whether the YooKassa code path is enabled, so it
    travels with the secret).

    ``None`` semantics: the corresponding webhook returns ``503`` and
    logs a single explanatory line. We intentionally do NOT crash on
    startup when a provider is unconfigured — operators routinely run
    the bot with a subset of providers enabled, and forcing all four
    secrets at boot would block dev/staging environments unnecessarily.
    The ``503`` (versus the legacy ``403``) signals "service degraded,
    retry later" instead of "you are unauthorised" — semantically
    correct: a missing secret is our problem, not the caller's.

    Why ``SecretStr``: keeps the secret out of log lines that dump the
    settings object. Mirrors how :class:`OpenAiConfig.api_key` and
    :class:`BotConfig.token` are stored.

    Env-var names (``CRYPTO_PAY_TOKEN``, ``YOOKASSA_SHOP_ID``,
    ``YOOKASSA_SECRET_KEY``, ``STRIPE_WEBHOOK_SECRET``) match what
    legacy ``bot.py`` and ``main.py`` already read so the same prod
    ``.env`` keeps working through the cutover.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    crypto_api_secret: SecretStr | None = Field(default=None, alias="CRYPTO_PAY_TOKEN")
    yookassa_shop_id: str | None = Field(default=None, alias="YOOKASSA_SHOP_ID")
    yookassa_secret: SecretStr | None = Field(default=None, alias="YOOKASSA_SECRET_KEY")
    stripe_webhook_secret: SecretStr | None = Field(default=None, alias="STRIPE_WEBHOOK_SECRET")
    # RollyPay issues TWO secrets per terminal and they are not
    # interchangeable: the api key authenticates our outbound calls
    # (``X-API-Key``), the signing secret verifies their inbound
    # callbacks (``X-Signature``). Swapping them yields a 401 on one
    # side and a signature mismatch on the other, so they get separate
    # fields rather than one "rollypay_secret".
    rollypay_api_key: SecretStr | None = Field(default=None, alias="ROLLYPAY_API_KEY")
    rollypay_signing_secret: SecretStr | None = Field(default=None, alias="ROLLYPAY_SIGNING_SECRET")

    @field_validator(
        "crypto_api_secret",
        "yookassa_shop_id",
        "yookassa_secret",
        "stripe_webhook_secret",
        "rollypay_api_key",
        "rollypay_signing_secret",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_unset(cls, v: object) -> object:
        """Treat an empty / whitespace payment credential as unset (#818).

        A commented-out knob left as a bare ``ROLLYPAY_API_KEY=`` in
        ``.env`` is the normal way operators disable a provider, and it
        yields ``SecretStr("")`` — which is *not* ``None``, so every
        ``…_configured`` property below reports the provider as ready.
        The failures land later and away from the cause: an empty
        ``X-API-Key`` gets a 401 from RollyPay when a user is already
        staring at a checkout button, and an empty signing secret makes
        :meth:`RollyPayAdapter.verify_signature` fail closed on its
        first branch, ``if not self._secret``, so every genuine
        callback is answered 403 — money taken by the provider and
        never credited in the bot.
        Normalising blank → ``None`` routes both back through the one
        "not configured" path, which is the branch that actually says so.

        Same idiom as :meth:`WebhookConfig._empty_secret_token_is_none`,
        but ``mode="before"``: these arrive as raw env strings, and the
        check has to happen before ``SecretStr`` wraps them.
        """
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def crypto_configured(self) -> bool:
        return self.crypto_api_secret is not None

    @property
    def yookassa_configured(self) -> bool:
        return self.yookassa_shop_id is not None and self.yookassa_secret is not None

    @property
    def stripe_configured(self) -> bool:
        return self.stripe_webhook_secret is not None

    @property
    def rollypay_configured(self) -> bool:
        """Both halves present — checkout AND crediting are usable.

        Deliberately an AND rather than two independent flags. Either
        half alone is a trap: the api key without the signing secret
        mints payments the bot can never credit (the user pays and the
        webhook 403s), and the signing secret without the api key
        credits payments nothing in the bot can create. Both failure
        modes cost the owner money or trust, so the method is offered
        only when the round trip actually closes.
        """
        return self.rollypay_api_key is not None and self.rollypay_signing_secret is not None


class StatsConfig(BaseSettings):
    """``/stats`` rendering knobs.

    ``period_days`` matches legacy's hardcoded 7-day window
    (``bot/preview/stats_preview.py``). Exposed as a setting so ops can
    widen the window without code change; the handler validates
    ``>= 1``.

    ``timezone`` is the named TZ used to compute "today". Legacy used
    server-local time via SQLite ``date('now')`` — undocumented and
    fragile across deploys. The new pipeline pins a TZ explicitly so
    the calendar boundary is deterministic.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    period_days: int = Field(default=7, alias="STATS_PERIOD_DAYS", ge=1, le=365)
    timezone: str = Field(default="Europe/Moscow", alias="STATS_TIMEZONE")


class GuideSiteConfig(BaseSettings):
    """Public HTML guide (``/commands``, ``/commands/en``) settings.

    The editor (GET/POST ``/commands/edit``) is mounted by the same
    router this flag controls, so ``enabled=False`` takes it down
    with the guide. This paragraph used to say the opposite — that
    the guide was read-only here and the editor stayed on Flask
    until the cutover, so its writes could not race the legacy
    handler. T-026 ported the editor and corrected that wording in
    :mod:`telegram_invite_bot.cms.guide_site`; this second copy of
    the claim was missed. Both halves answer 404 while
    ``GUIDES_EDIT_SECRET`` is unset, which is the state production
    is in — the surface is unchanged, the sentence about who serves
    it was not.

    ``enabled=False`` skips mounting the router entirely — useful for
    headless test runs and the small fraction of operators who run
    the bot without a public web surface. Default ``True`` matches
    legacy behaviour (the Flask wiring was unconditional).
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    enabled: bool = Field(default=True, alias="GUIDE_SITE_ENABLED")
    # Directory containing ``telegraph_guide_ru.md`` and ``_en.md``.
    # Default points at the repo root for legacy parity — the deploy
    # ships both files there. Override when restructuring assets.
    sources_dir: Path = Field(default=Path("."), alias="GUIDE_SITE_SOURCES_DIR")
    # The brand in the page header and footer. Defaults to this bot's
    # own name rather than a generic "Bot" so a deploy that forgets the
    # env var still ships a page that says who it belongs to.
    site_title: str = Field(default="ком17", alias="GUIDE_SITE_TITLE")


class LegalConfig(BaseSettings):
    """The public documents an acquiring bank asks to see, and the
    support contact printed in them.

    Three things have to exist, permanently reachable, before a payment
    provider's bank will sign off on a project: a privacy policy, a user
    agreement (public offer), and a way to reach support. The first two
    are shipped as text in :mod:`telegram_invite_bot.cms.legal.documents`
    — in git, not in a database and not on a third-party pastebin, so a
    deploy cannot serve a document nobody reviewed and no outside site
    can take them offline. What is left configurable is only what varies
    per operator: who the operator *is*, and how to reach them.

    Every field degrades rather than fails. An unset ``SUPPORT_USERNAME``
    does not produce a broken ``@`` or a dead ``t.me`` link — the contact
    block simply falls back to the in-bot ticket system (``/support``),
    which is itself one of the three contact forms the bank accepts.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    #: Telegram handle of the support account, with or without the ``@``.
    support_username: str | None = Field(default=None, alias="SUPPORT_USERNAME")
    #: Support mailbox. Optional — the bank accepts a handle OR an email
    #: OR a ticket system, and this project ships the ticket system.
    support_email: str | None = Field(default=None, alias="SUPPORT_EMAIL")
    #: Who is on the other side of the contract. Free-form: a personal
    #: name, a sole trader ("ИП Иванов И.И."), or a company. Falls back
    #: to the site title, which is honest about it being a project name
    #: rather than inventing a legal entity that does not exist.
    operator_name: str | None = Field(default=None, alias="LEGAL_OPERATOR_NAME")
    #: Registration details (ИНН / ОГРНИП / address), rendered verbatim
    #: under the operator name when present. Deliberately one free-text
    #: field rather than typed columns: what a bank wants to see here
    #: differs by jurisdiction and by the operator's legal form, and a
    #: schema that guesses wrong forces an operator to lie to satisfy it.
    operator_details: str | None = Field(default=None, alias="LEGAL_OPERATOR_DETAILS")

    @field_validator("support_username", mode="after")
    @classmethod
    def _clean_username(cls, value: str | None) -> str | None:
        """Strip the ``@`` and refuse anything that is not a handle.

        This value ends up in a ``https://t.me/...`` inline button. A
        stray space or a pasted full URL would render a link that fails
        at tap time — worse than no button, because a dead support link
        on a legal page is exactly what the reader needed to work.

        #1484 covered the same predicate on ``bot_username`` in the two
        CMS contexts; this is the third copy of it, and the one whose
        docstring most clearly overpromises. ``str.isalnum`` is
        Unicode-aware, so ``SUPPORT_USERNAME=@поддержка`` used to pass
        and mint exactly the dead button described above.
        """
        if value is None:
            return None
        handle = value.strip().lstrip("@")
        if not handle or not handle.isascii():
            return None
        if not all(c.isalnum() or c == "_" for c in handle):
            return None
        return handle

    @field_validator("support_email", mode="after")
    @classmethod
    def _clean_email(cls, value: str | None) -> str | None:
        """Shape check only — enough to keep a broken ``mailto:`` off the
        page without pretending to validate deliverability.
        """
        if value is None:
            return None
        email = value.strip()
        local, _, domain = email.partition("@")
        if not local or "." not in domain or any(c.isspace() for c in email):
            return None
        return email

    @property
    def support_url(self) -> str | None:
        """``https://t.me/<handle>``, or ``None`` when unconfigured."""
        return f"https://t.me/{self.support_username}" if self.support_username else None

    def operator(self, fallback: str) -> str:
        """The operator name to print, ``fallback`` (the site title) if
        none was configured.
        """
        name = (self.operator_name or "").strip()
        return name or fallback


class ThrottlingConfig(BaseSettings):
    """Per-user rate-limit knobs for the global throttling middleware.

    Token-bucket: each user gets a bucket of ``capacity`` tokens that
    refills at ``refill_per_second`` tokens/sec. Each Message /
    CallbackQuery costs one token; on empty bucket the update is
    silently dropped (no reply — replying to an abuser amplifies
    them, and Telegram doesn't surface mid-conversation throttle
    notices in any first-class way).

    Defaults chosen to be permissive for human use (burst of 10,
    sustained 2/sec ≈ 120/min) while still cutting off pathological
    flood-bots before they reach business logic. Tune in prod with
    the ``tib_throttled_total`` metric — sustained nonzero rate on
    a real user_id means tighten; constant zero means the bucket
    isn't doing anything.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    enabled: bool = Field(default=True, alias="THROTTLE_ENABLED")
    capacity: int = Field(default=10, alias="THROTTLE_CAPACITY", ge=1, le=1000)
    refill_per_second: float = Field(
        default=2.0, alias="THROTTLE_REFILL_PER_SECOND", gt=0.0, le=1000.0
    )
    # Cap the in-memory user→bucket map. Once exceeded, the least-recently-seen
    # user's bucket is evicted (LRU). Without a cap, a long-lived process
    # plus a churn of new user IDs would leak memory unboundedly.
    max_tracked_users: int = Field(
        default=10_000, alias="THROTTLE_MAX_TRACKED_USERS", ge=100, le=1_000_000
    )


class FsmStorageConfig(BaseSettings):
    """Backend selection for aiogram FSM storage (T-012).

    ``backend="memory"`` keeps the legacy/dev posture: state lives in
    process RAM, dies on restart. Cheap, no I/O, fine for unit tests
    and developer machines where a /cpc challenge spans seconds.

    ``backend="sqlite"`` points the dispatcher at
    :class:`telegram_invite_bot.fsm.sqlite_storage.SQLiteStorage` —
    flow state survives a restart. Wired separately from the business
    engines (``db/engines.py``) because the FSM file is ephemeral
    state, not a system of record, and we want a deploy-time
    ``rm database/fsm.db`` to be a safe recovery action without
    touching users.db / economy.db / etc.

    Default stays ``memory`` so an existing deployment that boots
    without setting ``FSM_BACKEND`` keeps its current behaviour — the
    SQLite path is opt-in until ops explicitly flip it. Once flipped,
    the sweeper picks up the new backend via the duck-typed
    ``iter_keys`` hook in ``scheduler/fsm_sweeper.py``.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    backend: str = Field(default="memory", alias="FSM_BACKEND")
    sqlite_path: Path = Field(default=Path("database/fsm.db"), alias="FSM_SQLITE_PATH")

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        # Hard list — a typo (``FSM_BACKEND=sqllite``) silently falling
        # back to ``memory`` would be the worst kind of bug: persistence
        # appears configured but isn't.
        allowed = {"memory", "sqlite"}
        normalised = value.strip().lower()
        if normalised not in allowed:
            raise ValueError(f"FSM_BACKEND must be one of {sorted(allowed)}, got {value!r}")
        return normalised


class EconomyConfig(BaseSettings):
    """Economy-side tunables ported per-stage from legacy ``bot_settings.json``.

    Stage 28 introduces ``referral_commission_percent`` only — the
    single number ``/referral`` renders to advertise the cut. Other
    economy knobs (daily-bonus amount, group-treasury minimums,
    coin-pack pricing, …) join this class as their handlers migrate.

    Legacy reads the same value from ``bot_settings.json`` via
    ``settings.get("referral_commission_percent", 10)``. The new
    pipeline reads from the environment so deployments don't have to
    sync a JSON file across hosts; default ``10`` mirrors the legacy
    fallback, and the matching env var name keeps ops muscle memory.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    referral_commission_percent: int = Field(
        default=10,
        alias="REFERRAL_COMMISSION_PERCENT",
        ge=0,
        le=100,
    )

    # #75: legacy DEVELOPER_COMMISSION_PERCENT (bot.py:3173, default 5) —
    # the developer wallet's cut of every purchase/top-up. Recipient is
    # the existing ADMIN_CHAT_ID (legacy dev_id, bot.py:9888).
    developer_commission_percent: int = Field(
        default=5,
        alias="DEVELOPER_COMMISSION_PERCENT",
        ge=0,
        le=100,
    )

    # RR-2 #14: legacy PURCHASE_DONATION_TO_GROUP_PERCENT (bot.py:2562,
    # default 15) — the slice of a *group* purchase that is routed to the
    # chosen group: it becomes group_xp on the donations leaderboard and
    # (minus the developer cut) coins for the group's creator
    # (bot.py:13240 → donation_from_purchase, bot.py:10715). ``0``
    # disables group routing entirely while leaving the chooser working.
    purchase_donation_to_group_percent: int = Field(
        default=15,
        alias="PURCHASE_DONATION_TO_GROUP_PERCENT",
        ge=0,
        le=100,
    )

    # #193: legacy ``coins_transfer_tax`` (bot.py:2550) — the cut a
    # /send takes off the top, expressed as a fraction 0..1 to keep
    # legacy's units (bot.py:10249 ``int(amount * effective_tax_rate)``).
    # Default ``0.0`` mirrors the legacy default verbatim, exactly like
    # every sibling field in this class, and matches the operator's own
    # ``settings.json``. The port used to hard-code ``0.05`` inside
    # :class:`TransferConfig` under a docstring claiming that was the
    # legacy default — it is not — so every /send charged a 5% tax
    # legacy never charged, and then burned it because the treasury
    # destination was never wired (middlewares/economy.py).
    coins_transfer_tax: float = Field(
        default=0.0,
        alias="COINS_TRANSFER_TAX",
        ge=0.0,
        le=1.0,
    )

    # #1946: legacy ``anti_abuse_new_user_no_daily_hours`` (bot.py:3880,
    # guard at bot.py:12024) — a freshly registered wallet cannot claim
    # ``/daily`` for this many hours, so an attacker minting throwaway
    # accounts to farm the bonus gets nothing out of them. The port
    # carried the arithmetic across (``utils.daily
    # .new_user_lockout_remaining``) and then never wired a caller, so
    # the guard did not exist in the new pipeline at all.
    #
    # Default ``0`` = disabled mirrors the legacy default verbatim, and
    # legacy gated the call site on ``> 0`` for exactly that reason —
    # this deployment never carried a ``settings.json`` that set it, so
    # switching the guard on is an operator decision, not a silent
    # behaviour change on the first restart after this ships.
    anti_abuse_new_user_no_daily_hours: int = Field(
        default=0,
        alias="ANTI_ABUSE_NEW_USER_NO_DAILY_HOURS",
        ge=0,
        le=8760,
    )

    # L-41: minimum /group_pay withdrawal from the group treasury
    # (legacy bot.py:2563 group_treasury_min_withdrawal=1000).
    group_treasury_min_withdrawal: int = Field(
        default=1000,
        alias="GROUP_TREASURY_MIN_WITHDRAWAL",
        ge=1,
    )

    # #2007: the bounds and the anti-spam gap of ``/donate`` — a member
    # donating coins to the group they are writing in. Legacy kept all
    # three as module constants at bot.py:10568-10570 (1, 1_000_000 and
    # 10 seconds, the defaults carried over unchanged; the cooldown was
    # spelled there with a shorter suffix than the alias below, so match
    # on the value, not the name). They are knobs here because the
    # ceiling is the only thing standing between a fat-fingered zero and
    # a group's whole leaderboard position.
    donate_min_amount: int = Field(
        default=1,
        alias="DONATE_MIN_AMOUNT",
        ge=1,
    )
    donate_max_amount: int = Field(
        default=1_000_000,
        alias="DONATE_MAX_AMOUNT",
        ge=1,
    )
    donate_cooldown_seconds: int = Field(
        default=10,
        alias="DONATE_COOLDOWN_SECONDS",
        ge=0,
    )

    # P2P D2: pending trades older than this are auto-cancelled and the
    # COM slice returns to the order (DESIGN_P2P.md §2.3).
    p2p_pending_ttl_minutes: int = Field(
        default=30,
        alias="P2P_PENDING_TTL_MINUTES",
        ge=1,
    )

    # #267: an unanswered PvP challenge holds the creator's stake, so it
    # expires MUCH sooner than a P2P trade. Legacy:
    # ``PVP_OFFER_TTL_SEC = 600`` (bot.py:3782), floored at 60s in the
    # sweep (bot.py:14801). The port reused ``p2p_pending_ttl_minutes``
    # (30) instead, tripling the hold on top of an hourly sweeper.
    pvp_offer_ttl_minutes: int = Field(
        default=10,
        alias="PVP_OFFER_TTL_MINUTES",
        ge=1,
    )

    # A-03: passive per-message earning in groups. Legacy gated this
    # globally (``COINS_ENABLED`` / ``COINS_MESSAGE_REWARD``) — there was
    # NO per-group feature flag — and throttled it in-process with the
    # ``should_reward_message`` heuristic (bot.py:43712). Defaults below
    # mirror the legacy constants verbatim (bot.py:2542-2559).
    coins_enabled: bool = Field(default=True, alias="COINS_ENABLED")
    coins_message_reward: int = Field(
        default=1,
        alias="COINS_MESSAGE_REWARD",
        ge=0,
    )
    message_reward_min_chars: int = Field(
        default=8,
        alias="MESSAGE_REWARD_MIN_CHARS",
        ge=0,
    )
    message_reward_cooldown_sec: int = Field(
        default=20,
        alias="MESSAGE_REWARD_COOLDOWN_SEC",
        ge=0,
    )
    message_reward_max_per_minute: int = Field(
        default=3,
        alias="MESSAGE_REWARD_MAX_PER_MINUTE",
        ge=1,
    )
    message_reward_duplicate_window_sec: int = Field(
        default=120,
        alias="MESSAGE_REWARD_DUPLICATE_WINDOW_SEC",
        ge=0,
    )
    # T-019 (docs/ECONOMY_RATE_AUDIT.md §2.1 / R1): the ceiling legacy
    # never had. The four knobs above throttle the *rate* of passive
    # earning but bound nothing over a day: at 3 rewards/minute the
    # per-day ceiling was 3 × 1440 = 4 320 COM — 4.8 USDT at the 900
    # COM/USDT withdrawal rate — for a script posting distinct
    # 8-character messages, and roughly double that with VIP
    # ``message_bonus`` and an ``xp_boost`` item stacked on top. That
    # alone very nearly funds the 10 000 COM/day withdrawal cap forever,
    # with no money ever entering the system.
    #
    # 150/day is well above what a talkative human accumulates through
    # the 20 s cooldown in a normal evening, and 28.8× below the old
    # ceiling. Set to 0 to disable the cap (restores legacy behaviour).
    message_reward_daily_cap: int = Field(
        default=150,
        alias="MESSAGE_REWARD_DAILY_CAP",
        ge=0,
    )

    # #26 claim-side subscription gate. A check row's
    # ``required_subscription`` column is a BOOL (0/1) — it records that
    # the creator wanted a subscription gate, but the prod schema does
    # NOT carry a per-check channel handle. The single channel every
    # gated check verifies against therefore lives here (legacy enforced
    # one global "наши каналы" subscription). Accepts a ``@username`` or
    # a numeric ``-100…`` chat id; when UNSET the gate has no channel to
    # verify against and is treated as a pass (the check still credits) —
    # the column alone can't name a channel. Set this env var to actually
    # enforce the gate.
    subscription_channel: str | None = Field(
        default=None,
        alias="CHECK_SUBSCRIPTION_CHANNEL",
    )

    @model_validator(mode="after")
    def _check_donate_band(self) -> EconomyConfig:
        """``donate_min_amount`` must not exceed ``donate_max_amount``.

        Same reasoning as :meth:`WithdrawConfig._check_band` (#1934): the
        handler enforces the two ends independently, so an inverted band
        does not fail loudly — it answers every single ``/donate`` with
        one of two bound refusals, and the operator sees a command that
        "stopped working" rather than a config error.
        """
        if self.donate_min_amount > self.donate_max_amount:
            raise ValueError(
                "DONATE_MIN_AMOUNT must not exceed DONATE_MAX_AMOUNT "
                f"(got {self.donate_min_amount} > {self.donate_max_amount})"
            )
        return self


class WithdrawConfig(BaseSettings):
    """``/withdraw`` (#28, T-027) limits + conversion knobs.

    The user-side flow escrows ``amount_com`` coins up-front and an
    admin approves the Crypto Pay payout. These four values gate and
    price that flow:

    * ``min_coins`` / ``max_coins`` — the accepted request band. Below
      the floor the payout's fixed network cost isn't worth it; above
      the ceiling a single request shouldn't be able to drain the app
      wallet without a manual top-up. #245(c): both defaults are new
      policy, not a port — an earlier revision of this docstring
      claimed they matched "the legacy P2P withdraw limits", and they
      do not. Legacy had no band at all: each of its three withdraw
      rails refused only ``amount <= 0`` and ``amount > balance``
      (``bot.py:20408``, ``:20466-20471``, ``:20606``). ``90000``
      appears nowhere in ``bot.py``; ``4500`` appears exactly once, and
      not as a limit — it is the rouble price of the year-long VIP shop
      entry (``bot.py:12357``). Both defaults stay,
      and both are env-tunable precisely because nothing about the
      numbers is inherited.
    * ``coins_per_usdt`` — the conversion rate. It *defaults* to the
      top-up rate (:data:`~telegram_invite_bot.services.payments.rates.COINS_PER_USD`)
      so a coins→USDT→coins round trip is value-neutral out of the box.
      ``amount_com / coins_per_usdt`` = USDT paid. Raising it via env
      opens a buy/sell spread — the owner's lever from
      ``docs/ECONOMY_RATE_AUDIT.md`` R6, deliberately left at parity in
      code so no rate changes without an explicit ops decision.
    * ``asset`` — the Crypto Pay asset transferred (``USDT`` by default).

    Exposed as env so ops can retune the band / rate without a code
    change; the service validates each request against them.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    min_coins: int = Field(default=4500, alias="WITHDRAW_MIN_COINS", ge=1, le=10_000_000)
    max_coins: int = Field(default=90_000, alias="WITHDRAW_MAX_COINS", ge=1, le=100_000_000)
    coins_per_usdt: float = Field(
        default=float(COINS_PER_USD), alias="WITHDRAW_COINS_PER_USDT", gt=0.0
    )
    asset: str = Field(default="USDT", alias="WITHDRAW_ASSET")
    # L-92: per-user rolling daily / monthly withdrawal caps (in coins).
    # The service derives current-period usage live from request
    # timestamps, so the cap self-resets at the cycle boundary.
    #
    # #245(c): the two halves have different provenance, and the equal
    # defaults hide that. The daily cap is a faithful port — legacy
    # stored it per user (``bot.py:5222``, ``DEFAULT 10000``) and
    # enforced it on every rail (``:20411``, ``:20472``, ``:20609``).
    # The monthly cap is new *enforcement* of an old number: legacy
    # declared ``monthly_limit_com INTEGER DEFAULT 100000``
    # (``bot.py:5223``), read it back (``:9983``) and zeroed its
    # counter each month (``:10023``) — but never once compared a
    # request against it. The column was decoration. Keeping the
    # default identical means the gate now binds exactly where legacy
    # always advertised it would.
    daily_limit_coins: int = Field(
        default=10_000, alias="WITHDRAW_DAILY_LIMIT_COINS", ge=1, le=100_000_000
    )
    monthly_limit_coins: int = Field(
        default=100_000, alias="WITHDRAW_MONTHLY_LIMIT_COINS", ge=1, le=1_000_000_000
    )
    # T-019 (docs/ECONOMY_RATE_AUDIT.md §6 / R2): only accounts that have
    # actually purchased coins may cash out. The caps above bound the
    # *rate* of payout but not its source — every coin the bot mints for
    # free (message rewards, daily bonus, promo codes, referral kickbacks)
    # was otherwise redeemable at ``coins_per_usdt``, so an account that
    # never paid anything could still draw on the owner's Crypto Pay
    # wallet up to 100 000 COM a month.
    #
    # This gate is a *threshold*, not a bound: it asks whether money ever
    # came in, not how much. ``payout_ratio`` below is what actually
    # bounds the total.
    #
    # Set false to drop the threshold (e.g. to honour balances accrued
    # before the gate shipped) — but note that on its own that no longer
    # restores pre-T-019 behaviour: ``payout_ratio`` below still binds.
    # Both knobs have to be off to reopen the old, unbounded door.
    require_deposit: bool = Field(default=True, alias="WITHDRAW_REQUIRE_DEPOSIT")
    # T-020 (docs/ECONOMY_RATE_AUDIT.md R6): the bound R2 was mistakenly
    # credited with. A user who deposits once clears ``require_deposit``
    # forever, and the near-zero house edge on the games meant they could
    # then convert 1 USDT of deposit into an arbitrarily large balance and
    # export all of it. Lifetime payout is now capped at
    # ``lifetime_deposits * payout_ratio`` coins.
    #
    # ``1.0`` = a user may cash out exactly what they paid in, never more:
    # the ecosystem cannot run at a loss on any individual account, while
    # every honest buyer keeps a full, unpenalised exit. Raise it to hand
    # back winnings above deposits (``1.5`` = up to 150 % of what they
    # paid); lower it to take a house cut on the way out. ``0`` disables
    # the cap entirely — same "0 means off" convention as
    # ``MESSAGE_REWARD_DAILY_CAP``.
    #
    # This is the reason no buy/sell spread ships (R6 as originally
    # drafted proposed 3 000 COM/USDT): a spread taxes the paying customer
    # to stop the grinder, whereas the cap stops the grinder and leaves
    # the paying customer whole. ``coins_per_usdt`` stays at parity.
    payout_ratio: float = Field(default=1.0, alias="WITHDRAW_PAYOUT_RATIO", ge=0.0, le=1000.0)

    @model_validator(mode="after")
    def _check_band(self) -> WithdrawConfig:
        """``min_coins`` must not exceed any of the ceilings above it —
        an inverted band would reject every possible request with a
        confusing message.

        #1934: the per-request ceiling (``max_coins``) was checked, the
        two period ceilings were not, even though the service enforces
        all three independently: ``amount < min_coins`` yields
        ``AMOUNT_TOO_SMALL`` (``withdraw_service.py:434``) and
        ``amount > quota.daily_remaining`` yields
        ``DAILY_QUOTA_EXCEEDED`` (``:479``). Set
        ``WITHDRAW_DAILY_LIMIT_COINS`` below ``WITHDRAW_MIN_COINS`` and
        the two gates close on each other: every amount is either too
        small or over quota, nobody can ever cash out, and the two
        refusals contradict each other so the operator debugs the
        service instead of the env file. Fail at load with the numbers
        named instead.

        Daily vs monthly needs no assertion of its own —
        ``WithdrawQuota.remaining`` takes ``min(...)`` of the two, so a
        monthly cap below the daily one merely binds earlier, which is
        a coherent (if unusual) configuration.
        """
        if self.min_coins > self.max_coins:
            raise ValueError(
                "WITHDRAW_MIN_COINS must be <= WITHDRAW_MAX_COINS "
                f"(got min={self.min_coins}, max={self.max_coins})"
            )
        if self.min_coins > self.daily_limit_coins:
            raise ValueError(
                "WITHDRAW_MIN_COINS must be <= WITHDRAW_DAILY_LIMIT_COINS, "
                "otherwise no amount is ever withdrawable "
                f"(got min={self.min_coins}, daily={self.daily_limit_coins})"
            )
        if self.min_coins > self.monthly_limit_coins:
            raise ValueError(
                "WITHDRAW_MIN_COINS must be <= WITHDRAW_MONTHLY_LIMIT_COINS, "
                "otherwise no amount is ever withdrawable "
                f"(got min={self.min_coins}, monthly={self.monthly_limit_coins})"
            )
        return self


class FeatureFlags(BaseSettings):
    """Toggles for the strangler migration."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    # T-023: when ON, the VIP-emoji-voice handler skips the wallet
    # debit entirely (out-of-band "gift" to VIP users — useful for
    # promo windows or VIP-tier upgrades where ops absorbs the cost).
    # Off by default: standard billing rules apply, VIPs pay just
    # like everyone else, only the VIP-gate itself is the perk.
    #
    # M-P-4: the gift is now gated by a per-user tier allowlist
    # (``unlimited_voice_tier_user_ids``). When the flag is ON,
    # ordinary VIPs still pay; only user IDs in the allowlist get
    # the free synthesis. An empty allowlist means "no one gets the
    # gift" even when the flag is ON — fail-closed so a forgotten
    # config change doesn't silently widen the gift cohort.
    vip_unlimited_voice: bool = Field(default=False, alias="VIP_UNLIMITED_VOICE")

    # M-P-4: comma-separated Telegram user IDs that constitute the
    # top VIP tier eligible for free /voice. Empty default → no
    # implicit top tier, so a deploy that forgets to set this
    # behaves like ``vip_unlimited_voice=False`` for everyone.
    unlimited_voice_tier_user_ids: str = Field(default="", alias="VIP_UNLIMITED_VOICE_USER_IDS")

    # RR-6 #72: kill switch for the online ``/joke`` sources (JokeAPI,
    # icanhazdadjoke, Lingva). Legacy carried the same flag. All three
    # are free, keyless third parties with no contract with us; when one
    # starts misbehaving (rate-limiting us, serving junk, hanging), ops
    # needs a way to fall back to the local pool that does not require a
    # redeploy. OFF by default — the online source is the feature.
    joke_offline_only: bool = Field(default=False, alias="JOKE_OFFLINE_ONLY")

    def parsed_unlimited_voice_tier_user_ids(self) -> frozenset[int]:
        """Return the comma-separated allowlist as a frozenset of ints.

        Same shape and validation posture as
        ``AiQuotaSettings.parsed_dev_user_ids`` — whitespace and
        empty pieces tolerated, malformed entries raise at call site.
        """
        if not self.unlimited_voice_tier_user_ids.strip():
            return frozenset()
        return frozenset(
            int(piece.strip())
            for piece in self.unlimited_voice_tier_user_ids.split(",")
            if piece.strip()
        )


class AiQuotaSettings(BaseSettings):
    """Per-tier daily AI-request ceilings (M-P-2).

    Backs the ``ai_daily_requests`` counter enforced by
    :class:`AiQuotaService`. Ceilings are operator-tunable so a
    promotion window ("everyone gets 20/day this weekend") doesn't
    need a code change. Reset is at midnight UTC (the date_iso
    column rolls over with the calendar day).

    Defaults: free=10/day, vip=200/day, dev=unlimited (via membership
    in ``dev_user_ids``). A ceiling of ``0`` means *unlimited* for that
    tier (no cap, no rejection, no counter write).

    VIP used to default to ``0`` for legacy parity (backlog L-69:
    ``_check_ai_daily_limit_non_vip`` let VIPs past the gate entirely).
    That parity is dropped on purpose. ``/ai``, ``/ask``, ``/voice`` and
    ``/quote`` charge the user NOTHING per request, so every call is
    billed to the operator's provider key and nothing but this counter
    stands between one VIP and an unbounded bill — the throttling
    middleware paces requests per second, not per day. The ceiling is
    deliberately generous (same reasoning, and the same number, as
    ``OPENAI_STT_GROUP_DAILY_LIMIT``): a real VIP does not ask 200
    questions a day, so the cap only ever bites abuse. Operators who
    genuinely want the old behaviour set ``AI_QUOTA_VIP_DAILY_LIMIT=0``.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    free_daily_limit: int = Field(default=10, alias="AI_QUOTA_FREE_DAILY_LIMIT", ge=0, le=10_000)
    # Generous but FINITE — 0 would mean unlimited, i.e. an unbounded
    # provider bill per VIP (see the class docstring).
    vip_daily_limit: int = Field(default=200, alias="AI_QUOTA_VIP_DAILY_LIMIT", ge=0, le=100_000)
    # Comma-separated list of Telegram user IDs that bypass the
    # counter entirely. Empty default → no implicit dev bypass in
    # prod (an unset env var means standard rules apply).
    dev_user_ids: str = Field(default="", alias="AI_QUOTA_DEV_USER_IDS")

    def parsed_dev_user_ids(self) -> frozenset[int]:
        """Return :attr:`dev_user_ids` parsed as a frozenset of ints.

        Tolerant of whitespace, empty entries, and ``""`` (empty env
        var → empty set). A malformed entry raises ``ValueError`` at
        call-time, not at startup, because the env var is operator-
        edited and we want a misconfigured value to be loud where
        it's used.
        """
        if not self.dev_user_ids.strip():
            return frozenset()
        return frozenset(
            int(piece.strip()) for piece in self.dev_user_ids.split(",") if piece.strip()
        )


class GamesConfig(BaseSettings):
    """Group game-message hygiene (RR-3 #33).

    Legacy auto-deleted every game reply after a TTL
    (``auto_delete_games=True`` / ``game_messages_ttl=30``,
    bot.py:2588-2589) via a per-message daemon thread
    (``schedule_deletion``, bot.py:6383). It was a blunt anti-spam
    measure: in a busy group ``/roll`` receipts pushed real
    conversation off the screen.

    We restore the capability but **invert the default**. Legacy's
    cards were one line; ours close with the balance, the freshly
    unlocked achievements and the remaining play allowance, and players
    scroll back to them. Silently shredding that 30 seconds later would
    be a richness regression of its own — so the sweep is opt-in, and a
    group that actually suffers from game spam turns it on.

    Deletion is group-only regardless of this flag: a DM has no spam
    problem, and a bot deleting a user's own private history is
    surprising. Telegram only lets a bot delete its own message within
    48h, which any sane TTL is well inside.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
    )

    auto_delete: bool = Field(default=False, alias="GAMES_AUTO_DELETE")
    # Legacy's 30s is the default *duration* once enabled; the lower
    # bound keeps a misconfigured "0" from deleting the card before
    # anyone can read it.
    ttl_seconds: int = Field(default=30, alias="GAMES_MESSAGE_TTL", ge=5, le=86_400)


class Settings(BaseSettings):
    """Top-level settings aggregate.

    Use :func:`get_settings` to obtain a cached instance. Tests can
    instantiate ``Settings(_env_file=...)`` directly or override via env vars.
    """

    # NOTE: ``env_prefix`` is here for exactly one reason — to take the
    # 21 section fields below OUT of the bare-name environment lookup.
    # They carry no alias (they are composed sub-models, not operator
    # knobs), so pydantic-settings looks each one up by its own field
    # NAME, case-insensitively. A host that exports ``AI``, ``HELP``,
    # ``BOT`` or ``WEBHOOK`` for something unrelated then has that
    # string fed in as the value of a whole section, and ``Settings()``
    # raises before the process can boot — naming a section the
    # operator never touched (#815). Our prod host is SHARED with
    # other services, so a stray short name is not hypothetical.
    # The prefix does not touch ``app_env``: pydantic-settings skips
    # prefixing for fields that declare an explicit alias. Nor does it
    # reach the sub-models — each is its own ``BaseSettings`` and
    # still reads ``BOT_TOKEN``, ``WEBHOOK_URL``, … unprefixed.
    # Same class of bug, same file, as the ``$PATH`` collision on the
    # ``WebhookConfig`` above.
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        env_prefix="SETTINGS_SECTION_",
    )

    app_env: AppEnv = Field(default=AppEnv.DEV, alias="APP_ENV")

    bot: BotConfig = Field(default_factory=BotConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    help: HelpConfig = Field(default_factory=HelpConfig)
    stats: StatsConfig = Field(default_factory=StatsConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    openai: OpenAiConfig = Field(default_factory=OpenAiConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    payments: PaymentsConfig = Field(default_factory=PaymentsConfig)
    throttling: ThrottlingConfig = Field(default_factory=ThrottlingConfig)
    guide_site: GuideSiteConfig = Field(default_factory=GuideSiteConfig)
    legal: LegalConfig = Field(default_factory=LegalConfig)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
    withdraw: WithdrawConfig = Field(default_factory=WithdrawConfig)
    fsm_storage: FsmStorageConfig = Field(default_factory=FsmStorageConfig)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    ai_quota: AiQuotaSettings = Field(default_factory=AiQuotaSettings)
    games: GamesConfig = Field(default_factory=GamesConfig)

    @property
    def is_prod(self) -> bool:
        return self.app_env is AppEnv.PROD

    @model_validator(mode="after")
    def _warn_on_stray_env_keys(self) -> Settings:
        """M-I-2: log a WARNING for env vars that match project prefixes
        but aren't recognised by any Settings sub-config.

        We keep ``extra="ignore"`` everywhere (flipping to ``forbid``
        would crash legacy deploys that carry old / sibling-project
        keys), but a typo'd env var like ``ENABLE_NEW_PIPELIN`` would
        otherwise be silently no-op'd. Logging at boot surfaces the
        misconfiguration without the brittleness of ``forbid``.
        """
        stray = _find_stray_env_keys(self)
        if stray:
            _stray_env_logger().warning(
                "Stray env vars matching known project prefixes (not recognised "
                "by Settings — likely typos): %s",
                ", ".join(sorted(stray)),
            )
        return self

    @model_validator(mode="after")
    def _require_secret_token_in_prod(self) -> Settings:
        """``WEBHOOK_SECRET_TOKEN`` MUST be set when ``APP_ENV=prod``.

        Without it, :func:`webhook.security.verify_secret_token` becomes
        a no-op and the bot will accept *any* POST to ``/webhook`` as a
        legitimate Telegram update — a textbook MITM/forgery vector.
        Fail fast at startup (Settings instantiation) rather than serving
        traffic with a broken auth check.

        Dev/staging keep the no-op behaviour: local runs through ngrok
        without a token are common. (This paragraph used to add "and the
        legacy ``main.py`` flow doesn't enforce it either" — there is no
        ``main.py`` since T-011, so the ngrok case is the whole reason.)
        """
        if self.app_env is AppEnv.PROD and self.webhook.secret_token is None:
            raise ValueError(
                "WEBHOOK_SECRET_TOKEN must be set when APP_ENV=prod "
                "(verify_secret_token would otherwise accept any caller)"
            )
        return self

    @model_validator(mode="after")
    def _webhook_secret_token_charset(self) -> Settings:
        """``WEBHOOK_SECRET_TOKEN`` must be what ``setWebhook`` accepts.

        The Bot API restricts the secret to 1-256 characters of
        ``A-Za-z0-9_-``. A value outside that set is not a stronger
        secret, it is a broken deployment whose failure mode is silent:
        ``setWebhook`` rejects the call, so the PREVIOUS deploy's
        registration stays live and every update keeps arriving with the
        OLD secret — which :func:`webhook.security.verify_secret_token`
        answers 403, forever, visible only as ``UPDATES_TOTAL`` with
        ``outcome="forbidden"`` climbing while the bot looks healthy. A
        non-ASCII value is worse still: ``compare_digest`` raises
        ``TypeError`` on every update, including Telegram's own.

        This lives on ``Settings`` rather than on ``WebhookConfig``
        precisely because it is about a secret. A ``field_validator``
        failure makes pydantic render ``input_value=<the raw token>``
        into the ``ValidationError``, and that text goes to journald on
        a failed deploy. Raising from the outer model instead leaves
        pydantic nothing to echo but the nested config's own repr, in
        which ``SecretStr`` is already masked.
        """
        token = self.webhook.secret_token
        if token is not None and _SECRET_TOKEN_RE.fullmatch(token.get_secret_value()) is None:
            raise ValueError(
                "WEBHOOK_SECRET_TOKEN must be 1-256 characters of A-Za-z0-9_- "
                "(Telegram Bot API restriction); setWebhook would reject it"
            )
        return self

    @model_validator(mode="after")
    def _require_webhook_url_in_prod(self) -> Settings:
        """``WEBHOOK_URL`` MUST be a non-empty https URL when ``APP_ENV=prod``.

        An empty value makes :func:`webhook.lifespan.setup_webhook` log
        a warning and skip the ``setWebhook`` call entirely — the bot
        boots cleanly, ``/healthz`` is green, ``/metrics`` increments,
        and yet Telegram never delivers a single update because no
        webhook is registered. The operator only notices when users
        report the bot is "dead."

        Failing at Settings load forces the misconfiguration into the
        startup log instead. Dev/staging keep the lazy behaviour so
        local polling / ngrok-driven runs aren't blocked.
        """
        if self.app_env is AppEnv.PROD and not self.webhook.url:
            raise ValueError(
                "WEBHOOK_URL must be set when APP_ENV=prod "
                "(setup_webhook would otherwise silently skip registration "
                "and Telegram would deliver no updates)"
            )
        return self


def _collect_known_aliases(model: type[BaseModel]) -> set[str]:
    """Recursively gather every env-var name (alias) registered on a
    Settings sub-tree.

    Walks ``model_fields``; for fields whose annotation is another
    ``BaseModel`` subclass, recurses. Falls back to the field name
    itself if no alias is set (case-insensitive comparison handles
    pydantic-settings' case folding).
    """
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        alias = field.alias
        if alias is not None:
            names.add(alias.upper())
        else:
            names.add(field_name.upper())

        annotation: Any = field.annotation
        # Unwrap ``Optional[X]`` / ``X | None`` style unions.
        # ``BaseModel`` subclasses appear as one of the union members.
        candidates: tuple[Any, ...] = (annotation,)
        if hasattr(annotation, "__args__"):
            candidates = candidates + tuple(annotation.__args__)
        for candidate in candidates:
            if (
                isinstance(candidate, type)
                and issubclass(candidate, BaseModel)
                and candidate is not BaseModel
            ):
                names |= _collect_known_aliases(candidate)
    return names


def _derive_env_prefix(alias: str) -> str | None:
    """Map one Settings alias to the env-var prefix it implies.

    ``COINS_TRANSFER_TAX`` -> ``COINS_``. Aliases with no underscore are
    the nested-model container fields (``ai``, ``economy``, ``paths``);
    they are never env vars themselves and a bare ``AI`` prefix would
    match unrelated variables like ``AI_AGENT``, so they yield ``None``.

    A first segment shorter than three characters is too generic to own
    on its own, so two segments are taken instead:
    ``AI_QUOTA_FREE_DAILY_LIMIT`` yields ``AI_QUOTA_``, not ``AI_``.
    """
    parts = alias.split("_")
    if len(parts) == 1:
        return None
    if len(parts[0]) < 3:
        return parts[0] + "_" + parts[1] + "_"
    return parts[0] + "_"


@lru_cache(maxsize=1)
def _known_env_prefixes() -> frozenset[str]:
    """Every env-var prefix the project owns, derived from Settings.

    Cached because the answer depends only on the class hierarchy, which
    is fixed once the module is imported. Deriving instead of listing is
    the point: a new field with a new prefix is covered the moment it is
    declared, where the old hand-written tuple silently was not.
    """
    prefixes = {
        prefix
        for alias in _collect_known_aliases(Settings)
        if (prefix := _derive_env_prefix(alias)) is not None
    }
    return frozenset(prefixes | set(_SEED_ENV_PREFIXES))


def _find_stray_env_keys(settings: Settings) -> set[str]:
    """Return env-var keys present in ``os.environ`` that match a known
    project prefix but are not recognised by any Settings sub-config.

    Pure function over ``os.environ`` and the Settings class hierarchy
    — exposed at module scope so tests can call it directly without
    triggering the logger.
    """
    known = _collect_known_aliases(type(settings)) | _NON_SETTINGS_ENV_KEYS
    prefixes = _known_env_prefixes()
    stray: set[str] = set()
    for raw_key in os.environ:
        upper = raw_key.upper()
        if upper in known:
            continue
        # Match only keys with a project-owned prefix to avoid spamming
        # operators about ``PATH``, ``HOME``, ``USER``, etc.
        if not any(upper.startswith(prefix) for prefix in prefixes):
            continue
        stray.add(upper)
    return stray


def _stray_env_logger() -> logging.Logger:
    """Stdlib logger for stray-env warnings.

    Stdlib (not loguru) deliberately — :func:`configure_logging` may not
    have run yet when ``Settings()`` is instantiated, and we still want
    the warning to surface. The stdlib root logger writes to stderr by
    default, and once ``configure_logging`` runs it intercepts stdlib
    handlers anyway.
    """
    return logging.getLogger("telegram_invite_bot.config.settings")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings instance.

    Do not call from request-scoped code — inject ``Settings`` via dishka
    instead. This factory exists so ``__main__.py`` and migrations can
    bootstrap without a container.
    """
    return Settings()
