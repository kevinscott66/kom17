"""Settings load correctly from env and reject missing required fields."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from telegram_invite_bot.config.settings import (
    AiConfig,
    AppEnv,
    HelpConfig,
    LogLevel,
    PaymentsConfig,
    Settings,
    WebhookConfig,
    WithdrawConfig,
    _collect_known_aliases,
    _derive_env_prefix,
    _known_env_prefixes,
)


@pytest.fixture(autouse=True)
def _clear_lru_cache() -> None:
    from telegram_invite_bot.config import settings as settings_module

    settings_module.get_settings.cache_clear()


def _drop_ambient_project_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every ambient variable a project prefix would match.

    The stray-key check reads the *real* ``os.environ``, so a test that
    asserts "nothing was flagged" is really asserting something about
    the machine it runs on. That is not a hypothetical: CI went red on a
    commit that touched none of this, because the GitHub runner exports
    ``ENABLE_RUNNER_TRACING`` and the seed prefix at the time was the
    bare verb ``ENABLE_``. The prefix has since been narrowed, but the
    next collision would be found the same way — by a red build on an
    unrelated change — unless the negative tests stop depending on the
    environment they happen to inherit.

    ``monkeypatch`` restores everything at teardown.
    """
    for key in list(os.environ):
        if any(key.upper().startswith(prefix) for prefix in _known_env_prefixes()):
            monkeypatch.delenv(key, raising=False)


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJ")
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("WEBHOOK_PATH", "/hook")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "x" * 32)  # required in prod
    monkeypatch.setenv(
        "WEBHOOK_URL", "https://example.test"
    )  # required in prod (validator added in cutover branch)

    settings = Settings(_env_file=None)

    assert settings.bot.token.get_secret_value().startswith("123456:")
    assert settings.app_env is AppEnv.PROD
    assert settings.is_prod is True
    assert settings.logging.level is LogLevel.WARNING
    assert settings.webhook.path == "/hook"


def test_settings_require_bot_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    # chdir to a clean dir so nested BaseSettings (each loads ``.env``
    # independently) can't find the dev .env at the repo root.
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_webhook_path_must_start_with_slash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("WEBHOOK_PATH", "hook-without-slash")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_paths_defaults_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.delenv("MESSAGE_STATS_DIR", raising=False)
    monkeypatch.delenv("SETTINGS_FILE", raising=False)
    monkeypatch.setenv("DATABASE_DIR", "/tmp/db")  # noqa: S108 — test fixture path

    settings = Settings(_env_file=None)
    assert str(settings.paths.resolved_message_stats_dir()) == "/tmp/db"
    assert str(settings.paths.resolved_settings_file()) == "/tmp/db/settings.json"


def test_prod_requires_webhook_secret_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """``APP_ENV=prod`` without ``WEBHOOK_SECRET_TOKEN`` must fail at
    Settings load. Otherwise :func:`verify_secret_token` would silently
    skip its check (``expected is None`` → early return) and the bot
    would accept any POST to ``/webhook`` as a real Telegram update —
    classic webhook-forgery surface that the bridge would happily
    forward to legacy handlers.

    Failing at Settings instantiation means a missing-secret prod deploy
    crashes loudly at process start (systemd will restart-loop and
    surface it) rather than running in a vulnerable state.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("WEBHOOK_SECRET_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="WEBHOOK_SECRET_TOKEN"):
        Settings(_env_file=None)


def test_dev_allows_missing_webhook_secret_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Dev/staging keep the no-op behaviour — local ngrok-driven runs
    routinely omit the secret, and forcing it would also break the
    legacy ``main.py`` coexistence path that doesn't enforce it.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("WEBHOOK_SECRET_TOKEN", raising=False)
    settings = Settings(_env_file=None)
    assert settings.webhook.secret_token is None


def test_prod_with_webhook_secret_token_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """The happy prod path: secret + URL present → Settings loads cleanly."""
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "s" * 32)
    monkeypatch.setenv("WEBHOOK_URL", "https://example.com")
    settings = Settings(_env_file=None)
    assert settings.is_prod
    assert settings.webhook.secret_token is not None


def test_ssl_cert_without_key_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """``SSL_CERT`` alone → ValidationError. Without this guard uvicorn
    silently falls back to HTTP and Telegram refuses to call the
    webhook — a debugging nightmare because the bot looks "up" but
    receives nothing.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("SSL_CERT", "/tmp/cert.pem")  # noqa: S108
    monkeypatch.delenv("SSL_KEY", raising=False)
    with pytest.raises(ValidationError, match="SSL_KEY"):
        Settings(_env_file=None)


def test_ssl_key_without_cert_rejected() -> None:
    """Symmetric of the above. Tested via ``WebhookConfig`` directly to
    avoid the env-var dance — both halves go through the same validator.
    """
    with pytest.raises(ValidationError, match="SSL_CERT"):
        WebhookConfig(SSL_KEY="/tmp/key.pem")  # noqa: S108


def test_ssl_both_or_neither_accepted() -> None:
    """Both halves set, or neither — both pass."""
    # Neither (default).
    WebhookConfig()
    # Both — pydantic coerces strings to Path automatically.
    cfg = WebhookConfig(SSL_CERT="/tmp/c.pem", SSL_KEY="/tmp/k.pem")  # noqa: S108
    assert cfg.ssl_cert is not None and cfg.ssl_key is not None


def test_prod_requires_webhook_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """An empty ``WEBHOOK_URL`` in prod is a silent-failure trap: the
    bot boots cleanly, ``/healthz`` returns 200, metrics increment —
    but ``setup_webhook`` skips ``setWebhook`` and Telegram never
    delivers a single update. Operators only notice when users
    complain. Fail at Settings load instead.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "s" * 32)
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    with pytest.raises(ValidationError, match="WEBHOOK_URL"):
        Settings(_env_file=None)


def test_dev_allows_empty_webhook_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Dev / polling-mode runs routinely omit WEBHOOK_URL — runner/polling
    doesn't need it. Forcing it would block local development.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.webhook.url == ""


def test_developer_ids_from_numbered_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy parity: ``DEVELOPER_ID_1..4`` populate the resolved set,
    duplicates collapse, gaps are allowed (only 1 and 3 set is valid).
    """
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("DEVELOPER_ID_1", "111")
    monkeypatch.setenv("DEVELOPER_ID_3", "333")
    monkeypatch.delenv("DEVELOPER_ID_2", raising=False)
    monkeypatch.delenv("DEVELOPER_ID_4", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)

    settings = Settings(_env_file=None)
    assert settings.bot.developer_ids == frozenset({111, 333})
    assert settings.bot.is_developer(111) is True
    assert settings.bot.is_developer(333) is True
    assert settings.bot.is_developer(222) is False


def test_developer_ids_fall_back_to_admin_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy parity: when ``DEVELOPER_ID_*`` are all unset, the bot owner
    is taken from ``ADMIN_CHAT_ID``. This is the path most dev ``.env``
    files at the repo root hit — they only set ADMIN_CHAT_ID and rely
    on the fallback.
    """
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    for i in (1, 2, 3, 4):
        monkeypatch.delenv(f"DEVELOPER_ID_{i}", raising=False)
    monkeypatch.setenv("ADMIN_CHAT_ID", "999")

    settings = Settings(_env_file=None)
    assert settings.bot.developer_ids == frozenset({999})
    assert settings.bot.is_developer(999) is True


def test_developer_ids_empty_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """No DEVELOPER_ID_*, no ADMIN_CHAT_ID → empty set. Critically
    ``is_developer(0)`` MUST be False — a zero owner-id in env would
    otherwise grant developer rights to any update whose ``from_user``
    is None (channel posts, anonymous admins).
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    for i in (1, 2, 3, 4):
        monkeypatch.delenv(f"DEVELOPER_ID_{i}", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)

    settings = Settings(_env_file=None)
    assert settings.bot.developer_ids == frozenset()
    assert settings.bot.is_developer(0) is False
    assert settings.bot.is_developer(12345) is False


def test_developer_ids_rejects_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd ``DEVELOPER_ID_2=0`` must NOT silently grant rights to
    user_id=0. We drop non-positive entries entirely — Telegram user IDs
    are always positive and any zero/negative value is operator error.
    """
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("DEVELOPER_ID_1", "111")
    monkeypatch.setenv("DEVELOPER_ID_2", "0")
    monkeypatch.delenv("DEVELOPER_ID_3", raising=False)
    monkeypatch.delenv("DEVELOPER_ID_4", raising=False)

    settings = Settings(_env_file=None)
    assert settings.bot.developer_ids == frozenset({111})
    assert settings.bot.is_developer(0) is False


def test_developer_ids_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The returned set is a ``frozenset`` (immutable). A handler that
    accidentally tries to ``.add()`` to it gets an AttributeError at
    once, not a silent runtime permission escalation.
    """
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("DEVELOPER_ID_1", "111")

    settings = Settings(_env_file=None)
    assert isinstance(settings.bot.developer_ids, frozenset)


def test_warns_on_stray_env_keys_matching_known_prefixes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-I-2: env vars that look like project settings but aren't
    mapped to any field (typos like ``ENABLE_NEW_PIPELIN`` or
    ``BOT_TOKNE``) must produce a WARNING on Settings load.

    Without this, ``extra="ignore"`` silently swallows the typo and
    the operator believes a feature flag is on when it isn't.
    """
    import logging

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("BOT_BOGUS_FIELD", "1")  # typo'd, matches BOT_ prefix
    monkeypatch.setenv("ENABLE_NEW_PIPELIN", "true")  # the canonical bug shape

    with caplog.at_level(logging.WARNING, logger="telegram_invite_bot.config.settings"):
        Settings(_env_file=None)

    msgs = [record.getMessage() for record in caplog.records]
    combined = "\n".join(msgs)
    assert "BOT_BOGUS_FIELD" in combined
    assert "ENABLE_NEW_PIPELIN" in combined


def test_no_warning_for_recognised_env_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-I-2 (negative): legitimate, mapped env vars must NOT trigger
    the stray-key warning — otherwise every well-configured deploy
    would log a spurious WARN on every boot.
    """
    import logging

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    _drop_ambient_project_env(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("APP_ENV", "dev")

    with caplog.at_level(logging.WARNING, logger="telegram_invite_bot.config.settings"):
        Settings(_env_file=None)

    for record in caplog.records:
        # The warning specifically about strays must not fire when
        # all keys are known. Other warnings (if any) are fine.
        assert "Stray env vars" not in record.getMessage()


def test_stray_check_covers_a_prefix_the_old_hand_written_tuple_lacked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1370 regression: the prefix set is derived from Settings, so a
    section added after M-I-2 was written is covered too.

    ``COINS_`` is one of twenty-four prefixes the hand-maintained tuple
    had drifted behind, which meant a typo'd ``COINS_TRANSFER_TAKS``
    was swallowed in silence and the bot ran on the default tax rate
    while the operator believed their override had taken effect.
    """
    import logging

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("COINS_TRANSFER_TAKS", "5")

    with caplog.at_level(logging.WARNING, logger="telegram_invite_bot.config.settings"):
        Settings(_env_file=None)

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "COINS_TRANSFER_TAKS" in combined


def test_section_container_names_do_not_become_prefixes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deriving prefixes from every alias would hand the two-letter
    section ``ai`` the prefix ``AI_``, and an unrelated ``AI_AGENT`` in
    the environment would be reported as a project typo on every boot.

    :func:`_derive_env_prefix` drops single-segment aliases for exactly
    this reason, and takes two segments when the first is very short —
    so the real ``AI_QUOTA_*`` fields are still covered.
    """
    import logging

    assert _derive_env_prefix("AI") is None
    assert _derive_env_prefix("AI_QUOTA_FREE_DAILY_LIMIT") == "AI_QUOTA_"
    assert _derive_env_prefix("COINS_TRANSFER_TAX") == "COINS_"

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    _drop_ambient_project_env(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("AI_AGENT", "someone-elses-tool")

    with caplog.at_level(logging.WARNING, logger="telegram_invite_bot.config.settings"):
        Settings(_env_file=None)

    for record in caplog.records:
        assert "Stray env vars" not in record.getMessage()


def test_every_settings_env_var_is_covered_by_a_known_prefix() -> None:
    """The drift that #1370 fixed can never come back: every alias that
    is actually an env var must match some prefix the check knows.

    Container aliases are excluded: a single-segment one (``ai``) is
    never an env var, and a multi-segment one (``ai_quota``) shows up
    as the prefix ``AI_QUOTA_`` rather than as a match for itself.
    """
    prefixes = _known_env_prefixes()
    uncovered = sorted(
        alias
        for alias in _collect_known_aliases(Settings)
        if "_" in alias
        and alias + "_" not in prefixes
        and not any(alias.startswith(prefix) for prefix in prefixes)
    )
    assert uncovered == []


# ── HelpConfig.guide_url (RR-6 #70) ─────────────────────────────────


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("ru", "https://guide.test/ru"),
        ("en", "https://guide.test/en"),
        # Unknown language falls back to Russian, matching i18n's own
        # fallback — the button must not link to a guide written in a
        # different language than the card around it.
        ("de", "https://guide.test/ru"),
    ],
)
def test_help_guide_url_resolves_per_language(lang: str, expected: str) -> None:
    config = HelpConfig(
        TELEGRAPH_COMMANDS_URL="https://guide.test/ru",
        TELEGRAPH_COMMANDS_URL_EN="https://guide.test/en",
    )
    assert config.guide_url(lang) == expected


@pytest.mark.parametrize("url", ["", "telegra.ph/guide", "javascript:alert(1)", "ftp://x/y"])
def test_help_guide_url_rejects_non_http_values(url: str) -> None:
    """A URL button with a scheme Telegram won't take fails the whole
    ``sendMessage``, so an operator typo must cost the button, not the
    command. Everything that isn't http(s) degrades to "no button".
    """
    config = HelpConfig(TELEGRAPH_COMMANDS_URL=url, TELEGRAPH_COMMANDS_URL_EN=url)
    assert config.guide_url("ru") is None
    assert config.guide_url("en") is None


def test_help_guide_url_none_when_unset() -> None:
    config = HelpConfig(TELEGRAPH_COMMANDS_URL=None, TELEGRAPH_COMMANDS_URL_EN=None)
    assert config.guide_url("ru") is None
    assert config.guide_url("en") is None


def _section_field_names() -> list[str]:
    """The ``Settings`` fields whose value is a nested settings model.

    Derived from ``model_fields`` rather than hard-coded so a section
    added later is covered without anyone remembering to extend a list.
    """
    return [
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseSettings)
    ]


def test_settings_has_the_expected_number_of_sections() -> None:
    """Guards the parametrisation below against silently collecting nothing."""
    assert len(_section_field_names()) == 21


@pytest.mark.parametrize("section", _section_field_names())
def test_stray_env_var_named_like_a_section_does_not_break_the_load(
    section: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """A shell variable that happens to be named like a section is ignored.

    #815: the section fields carry no alias, so pydantic-settings looks
    each one up by its bare field NAME, case-insensitively. Before the
    ``env_prefix`` on ``Settings.model_config``, a host exporting
    ``AI``, ``HELP``, ``BOT`` or ``WEBHOOK`` for anything unrelated fed
    that string in as the value of a whole section and ``Settings()``
    raised — the bot could not boot, and the error named a section the
    operator had never touched. Our prod host is shared with a mail
    server, so a stray short name is not hypothetical.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv(section.upper(), "1")

    settings = Settings(_env_file=None)

    field = Settings.model_fields[section]
    assert isinstance(getattr(settings, section), field.annotation)  # type: ignore[arg-type]


def test_app_env_still_reads_its_alias_under_the_section_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """``env_prefix`` must not reach the one aliased top-level field.

    pydantic-settings skips prefixing for fields that declare an
    explicit alias, so ``APP_ENV`` keeps working unprefixed. If that
    ever changes, prod boots as ``dev`` — no secret-token requirement,
    no webhook-URL requirement — which is exactly the silent downgrade
    the prod validators exist to prevent.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "x" * 32)
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test")

    assert Settings(_env_file=None).app_env is AppEnv.PROD


def test_nested_configs_still_read_their_own_env_vars_unprefixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """The prefix is scoped to ``Settings``, not inherited by sections.

    Each section is its own ``BaseSettings`` built by ``default_factory``
    with its own (empty) prefix, so the documented env-var names in
    ``.env.example`` keep working verbatim.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("WEBHOOK_PATH", "/hook")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    settings = Settings(_env_file=None)

    assert settings.webhook.path == "/hook"
    assert settings.logging.level is LogLevel.WARNING


# Every payment credential, paired with the field it lands in. The
# ``…_configured`` properties are not enough on their own: two of them
# are ANDs over a pair of fields, so blanking one half still reads
# False for the reason the *other* half is unset. The field itself is
# what has to become ``None``.
_PAYMENT_CREDENTIALS: tuple[tuple[str, str], ...] = (
    ("CRYPTO_PAY_TOKEN", "crypto_api_secret"),
    ("YOOKASSA_SHOP_ID", "yookassa_shop_id"),
    ("YOOKASSA_SECRET_KEY", "yookassa_secret"),
    ("STRIPE_WEBHOOK_SECRET", "stripe_webhook_secret"),
    ("ROLLYPAY_API_KEY", "rollypay_api_key"),
    ("ROLLYPAY_SIGNING_SECRET", "rollypay_signing_secret"),
)


@pytest.mark.parametrize(
    ("alias", "field"), _PAYMENT_CREDENTIALS, ids=[a for a, _ in _PAYMENT_CREDENTIALS]
)
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_payment_credential_reads_as_unset(
    alias: str,
    field: str,
    blank: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """#818: a bare ``ROLLYPAY_API_KEY=`` in ``.env`` means "off".

    Without the normaliser it produced ``SecretStr("")``, which is not
    ``None``, so the ``…_configured`` properties reported the provider
    as ready and the failure surfaced far from the cause — a 401 from
    the provider at checkout, or a 403 on every genuine callback (the
    adapter fails closed on an empty secret) with the money already
    taken.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv(alias, blank)

    config = PaymentsConfig(_env_file=None)

    assert getattr(config, field) is None
    assert config.crypto_configured is False
    assert config.yookassa_configured is False
    assert config.stripe_configured is False
    assert config.rollypay_configured is False


def test_non_blank_payment_credentials_survive_the_normaliser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """The blank check must not eat real values — including padded ones.

    Surrounding whitespace is preserved rather than stripped: only a
    *wholly* blank value means "unset", and silently trimming a real
    secret would turn an operator's copy-paste artefact into a
    signature mismatch that looks like a wrong key.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("ROLLYPAY_API_KEY", "live-key")
    monkeypatch.setenv("ROLLYPAY_SIGNING_SECRET", " padded ")

    config = PaymentsConfig(_env_file=None)

    assert config.rollypay_configured is True
    assert config.rollypay_api_key is not None
    assert config.rollypay_api_key.get_secret_value() == "live-key"
    assert config.rollypay_signing_secret is not None
    assert config.rollypay_signing_secret.get_secret_value() == " padded "


# ── WEBHOOK_SECRET_TOKEN must match Telegram's own charset ────────────

#: Every shape Telegram's ``setWebhook`` rejects, with the reason it is
#: worth a separate case. A space and a dot are what an operator's own
#: passphrase looks like; the Cyrillic one is the shape that makes
#: ``compare_digest`` raise on every update rather than merely fail.
_ILLEGAL_SECRET_TOKENS: tuple[tuple[str, str], ...] = (
    ("has space", "space"),
    ("has.dot", "dot"),
    ("has/slash", "slash"),
    ("ключ", "non-ascii"),
    ("x" * 257, "too-long"),
)


@pytest.mark.parametrize(
    "value", [v for v, _ in _ILLEGAL_SECRET_TOKENS], ids=[i for _, i in _ILLEGAL_SECRET_TOKENS]
)
def test_webhook_secret_token_rejects_what_telegram_rejects(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """Fail at settings load, not silently at every update.

    ``setWebhook`` refuses a secret outside ``A-Za-z0-9_-`` (1-256
    chars), and the refusal is invisible: the call fails, the previous
    deploy's registration stays live, and every update then arrives
    carrying the OLD secret — which ``verify_secret_token`` answers 403
    to, forever, while the process looks perfectly healthy. Rejecting
    the value here converts that into one loud startup error.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", value)
    with pytest.raises(ValidationError, match="WEBHOOK_SECRET_TOKEN"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["aZ0_-aZ0_-", "x" * 256])
def test_webhook_secret_token_accepts_the_whole_telegram_charset(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """The guard must not be narrower than Telegram itself.

    Underscore and hyphen are legal, and a 256-character secret is the
    documented maximum — refusing either would turn a working
    deployment into a boot failure.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", value)
    assert Settings(_env_file=None).webhook.secret_token is not None


def test_blank_webhook_secret_token_still_normalises_before_the_charset_check() -> None:
    """Validator order is load-bearing.

    ``_empty_secret_token_is_none`` runs first, so a bare
    ``WEBHOOK_SECRET_TOKEN=`` keeps meaning "unset" (a no-op in dev, a
    hard prod failure) instead of being rejected as an illegal charset,
    which would tell the operator the wrong thing to fix.
    """
    assert WebhookConfig(_env_file=None, WEBHOOK_SECRET_TOKEN="").secret_token is None
    assert WebhookConfig(_env_file=None, WEBHOOK_SECRET_TOKEN="   ").secret_token is None


def test_webhook_secret_token_error_never_echoes_the_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    """A rejected secret is still a secret.

    This is why the charset check is a ``model_validator`` on
    ``Settings`` and not a ``field_validator`` on ``WebhookConfig``: a
    field-level failure makes pydantic render
    ``input_value=<the raw token>`` into the ``ValidationError``, and
    that text goes straight to journald on a failed deploy. Raised from
    the outer model there is nothing to echo but the nested config's
    repr, where ``SecretStr`` is already masked.
    """
    marker = "QQQ-secret-marker-7777.leak"
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", marker)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert marker not in str(exc.value)


def test_deepseek_max_tokens_default_matches_legacy() -> None:
    """#1661: the ``/ask`` answer ceiling stays at the monolith's 1000.

    ``model`` and ``temperature`` were carried across from the
    monolith's settings dict (``bot.py:2758``) verbatim; ``max_tokens``
    arrived as 1024, which reads as rounding to a power of two during
    the port rather than a decision. It is also the per-answer ceiling
    on a metered API, and prod sets no ``DEEPSEEK_MAX_TOKENS``, so this
    default is what actually bills the operator — worth pinning so the
    next round trip through this file does not quietly widen it again.

    The knob itself is untouched: an operator who wants longer answers
    sets the env var.
    """
    config = AiConfig(_env_file=None)
    assert config.max_tokens == 1000
    assert config.model == "deepseek-chat"
    assert config.temperature == 0.7
    # Still a knob, not a constant.
    assert AiConfig(_env_file=None, DEEPSEEK_MAX_TOKENS=4096).max_tokens == 4096


def test_webhook_port_zero_is_rejected() -> None:
    """#1933: ``PORT=0`` is a legal ``bind()`` argument that asks the OS
    for a free ephemeral port. The process then comes up, stays alive
    and answers ``/healthz`` — on a port nobody routes to. nginx keeps
    proxying to 8080 into the void, so every liveness signal we have
    says "healthy" while not a single update is served: the exact
    alive-but-not-serving posture #1929 exists to prevent, arriving
    through the one door that check cannot see.

    Out-of-range values used to surface much later as an opaque socket
    error; now the offending env var is named at Settings load.
    """
    with pytest.raises(ValidationError, match="PORT"):
        WebhookConfig(_env_file=None, PORT=0)
    with pytest.raises(ValidationError, match="PORT"):
        WebhookConfig(_env_file=None, PORT=-1)
    with pytest.raises(ValidationError, match="PORT"):
        WebhookConfig(_env_file=None, PORT=65536)
    # Still a knob: the whole legal range stays open.
    assert WebhookConfig(_env_file=None, PORT=1).port == 1
    assert WebhookConfig(_env_file=None, PORT=65535).port == 65535
    assert WebhookConfig(_env_file=None).port == 8080


def test_webhook_host_defaults_to_loopback() -> None:
    """The default must not be bind-all.

    ``/metrics`` and ``/readyz`` are not authenticated by the
    application — what keeps them off the public internet is an nginx
    prefix ACL, and nginx can only refuse what has to pass through it.
    A socket bound to every interface is a second front door with no
    ACL on it, and on a host whose firewall was never configured (our
    own provisioning script sets none) that door is open.

    Loopback as the default inverts who has to be right: a deployment
    that genuinely needs a wider bind says so, and the operator who
    never thought about it gets the safe posture instead of the
    convenient one. Nothing real is lost — every supported deployment
    proxies to 127.0.0.1, and the container that does need bind-all
    sets ``HOST`` itself (see ``tests/unit/test_dockerfile.py``).
    """
    assert WebhookConfig(_env_file=None).host == "127.0.0.1"
    # Still a knob, and an explicit bind-all still works.
    assert WebhookConfig(_env_file=None, HOST="0.0.0.0").host == "0.0.0.0"  # noqa: S104
    assert WebhookConfig(_env_file=None, HOST="10.0.0.7").host == "10.0.0.7"


def test_withdraw_min_above_a_period_cap_is_rejected() -> None:
    """#1934: the three ceilings are enforced independently by
    ``WithdrawService`` — ``amount < min_coins`` is ``AMOUNT_TOO_SMALL``
    and ``amount > daily_remaining`` is ``DAILY_QUOTA_EXCEEDED`` — so a
    daily (or monthly) cap below the minimum closes both gates on each
    other and makes *every* amount unwithdrawable, with two refusals
    that contradict one another. Only the per-request band was checked.
    """
    with pytest.raises(ValidationError, match="WITHDRAW_DAILY_LIMIT_COINS"):
        WithdrawConfig(_env_file=None, WITHDRAW_MIN_COINS=5000, WITHDRAW_DAILY_LIMIT_COINS=4000)
    with pytest.raises(ValidationError, match="WITHDRAW_MONTHLY_LIMIT_COINS"):
        WithdrawConfig(
            _env_file=None,
            WITHDRAW_MIN_COINS=5000,
            WITHDRAW_DAILY_LIMIT_COINS=5000,
            WITHDRAW_MONTHLY_LIMIT_COINS=4000,
        )
    # The pre-existing per-request assertion is untouched.
    with pytest.raises(ValidationError, match="WITHDRAW_MAX_COINS"):
        WithdrawConfig(_env_file=None, WITHDRAW_MIN_COINS=5000, WITHDRAW_MAX_COINS=4000)
    # A monthly cap below the daily one stays legal: ``WithdrawQuota.remaining``
    # takes ``min(...)``, so it merely binds earlier.
    cfg = WithdrawConfig(
        _env_file=None, WITHDRAW_DAILY_LIMIT_COINS=50_000, WITHDRAW_MONTHLY_LIMIT_COINS=20_000
    )
    assert cfg.monthly_limit_coins == 20_000
    # Defaults are, and must stay, a coherent band.
    defaults = WithdrawConfig(_env_file=None)
    assert defaults.min_coins <= defaults.daily_limit_coins <= defaults.monthly_limit_coins


def test_no_known_prefix_stops_mid_word() -> None:
    """Every prefix must end at a segment boundary.

    :func:`_derive_env_prefix` appends the underscore itself, so the
    derived half of the set cannot break this; the seed list is
    hand-written and once did. ``HOST`` was seeded as a bare name and
    therefore matched ``HOSTNAME``, which every container runtime
    exports — the shipped image announced the operator's own hostname
    as a suspected typo of ours on every boot.
    """
    stops_mid_word = sorted(p for p in _known_env_prefixes() if not p.endswith("_"))
    assert not stops_mid_word, (
        "a prefix that does not end in '_' matches the middle of unrelated"
        f" variable names: {stops_mid_word}"
    )


def test_foreign_env_vars_are_not_claimed_as_project_typos() -> None:
    """The warning says "likely typos"; it must only say it about ours.

    These are real variables set by other software on the very hosts
    this bot runs on — a GitHub Actions runner, a Docker container, a
    login shell. Each one was, or nearly was, reported to the operator
    as a misspelling of a project setting. The check is only worth
    having while its output can be trusted, so the cost of a false
    positive here is the whole feature.
    """
    prefixes = _known_env_prefixes()
    foreign = (
        "ENABLE_RUNNER_TRACING",  # GitHub Actions runner — this one turned CI red
        "HOSTNAME",  # every container runtime
        "HOSTTYPE",  # bash
        "AI_AGENT",  # the collision `_derive_env_prefix` was written for
        "PORTAGE_TMPDIR",
        "ADMINISTRATOR",
        "CHECKPOINT_DISABLE",  # HashiCorp tooling
        "GROUPS",  # bash
        "LOGNAME",  # POSIX
        "GROUPSHARE",
    )
    claimed = sorted(
        name for name in foreign if any(name.startswith(prefix) for prefix in prefixes)
    )
    assert not claimed, f"the prefix set claims variables that belong to other software: {claimed}"
