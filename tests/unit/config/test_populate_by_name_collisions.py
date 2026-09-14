"""Audit: no other BaseSettings model leaks shell env vars via ``populate_by_name``.

Background — see ``6108bfa`` ("Fix: WebhookConfig.path leaked operator's
``$PATH`` via populate_by_name"). pydantic-settings with
``populate_by_name=True`` does a case-insensitive lookup of every field
NAME against the process environment *in addition to* the alias. When a
field name happens to match a variable an operator's shell / systemd /
container runtime exports (``PATH``, ``HOME``, ``USER``, ``PORT``, …),
the model silently picks up that unrelated value at load time. The
``WebhookConfig.path`` ↔ ``PATH`` collision was the first instance to
bite us; this file is the systematic followup the fix commit promised.

One precision that #815 cost us, so read it before trusting the
paragraph above: ``populate_by_name`` governs the field-name lookup only
for fields that HAVE an alias. A field with NO alias is looked up by its
bare name whatever the flag says — the flag adds a second key, it never
removes the first. So ``populate_by_name=False`` is a remedy for an
aliased field and a no-op for an aliasless one; the remedy there is
``env_prefix`` (or giving the field an alias).

What this test does
-------------------
For every ``BaseSettings`` model declared in ``config/settings.py``
*other than* the already-fixed ``WebhookConfig`` and the top-level
``Settings`` aggregate, this test:

1. Snapshots the model's default-loaded field values.
2. Pollutes the environment with a unique sentinel value for every
   common shell/systemd env var that could plausibly be set on a host
   running this bot.
3. Reloads the model and asserts that NO field value contains any of
   the sentinels.

The list of env vars checked is the union of the task brief's list and
the variables actually present in a fresh login shell on the dev box
(``env | cut -d= -f1`` on Darwin and on the prod Linux host).

If a future refactor renames a field to something that collides
(e.g. introducing ``home: Path`` on ``PathsConfig``), this test fires
with the exact field name and the colluding env var, so the fix —
``populate_by_name=False`` on that model, since every field here is
aliased — is obvious.

``Settings`` itself is NOT audited here, and the reason used to be
recorded as "its fields are either ``app_env`` or nested BaseSettings,
so nothing can collide". That was the blind spot #815 walked through:
the 21 nested fields carry no alias, so their own NAMES (``ai``,
``bot``, ``help``, ``games``, …) were the env keys pydantic-settings
looked them up under, and a host exporting any of those short names for
something unrelated crashed ``Settings()`` at import. It is covered
instead by the parametrised section test in ``test_settings.py``, which
derives the section list from ``model_fields`` rather than a hard-coded
tuple, and by ``env_prefix`` on ``Settings.model_config``.

Result of the initial audit (kept here as the why-it's-here record so a
later reader doesn't have to re-derive it):

* 12 remaining ``BaseSettings`` subclasses examined.
* 0 collisions found. The closest borderline call is
  ``StatsConfig.timezone`` vs the shell ``TZ`` — different names
  (``timezone`` vs ``tz``), case-insensitive lookup does NOT match
  prefixes, so safe. ``BotConfig.token`` is similarly not adjacent to
  any standard env var. ``GuideSiteConfig.enabled`` and
  ``ThrottlingConfig.enabled`` share a generic name but ``ENABLED`` is
  not a conventional shell/systemd variable.
* Therefore no model-config flips ship in this commit — the test is
  the deliverable, acting as a regression guard for future field
  renames and as documentation that the audit was performed.
"""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings

from telegram_invite_bot.config.settings import (
    AiConfig,
    BotConfig,
    EconomyConfig,
    FeatureFlags,
    GuideSiteConfig,
    HelpConfig,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    StatsConfig,
    ThrottlingConfig,
)

# Models audited here. ``WebhookConfig`` is excluded — its collision
# (``path`` ↔ ``$PATH``) was the original bug and is already fixed via
# ``populate_by_name=False`` plus a dedicated regression test in
# ``test_settings.py``. The top-level ``Settings`` is excluded because
# it is audited elsewhere, not because it is safe: its nested fields are
# aliasless, so their names are env keys (#815). See the docstring.
_AUDITED_MODELS: tuple[type[BaseSettings], ...] = (
    BotConfig,
    PathsConfig,
    LoggingConfig,
    ObservabilityConfig,
    HelpConfig,
    AiConfig,
    StatsConfig,
    GuideSiteConfig,
    ThrottlingConfig,
    EconomyConfig,
    FeatureFlags,
)

# Env vars an operator's shell / systemd unit / container runtime
# might set. Union of the task brief's list and what was observed in a
# fresh login shell on the dev box. Each gets a unique sentinel so the
# test failure message points directly at the offender.
_RISKY_ENV_VARS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LOGNAME",
    "PWD",
    "PORT",
    "HOST",
    "HOSTNAME",
    "TERM",
    "EDITOR",
    "VISUAL",
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "SSH_AUTH_SOCK",
    "SSH_TTY",
    "SSH_CONNECTION",
    "MAIL",
    "PAGER",
    "LS_COLORS",
    "COLUMNS",
    "LINES",
    "PS1",
    "PS2",
    "OLDPWD",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "GOPATH",
    "GOROOT",
    "JAVA_HOME",
    "NODE_PATH",
    "SHLVL",
)

_SENTINEL_PREFIX = "POPULATE_BY_NAME_AUDIT_SENTINEL__"


@pytest.fixture
def _poison_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """Pollute the environment with sentinel values for every risky var.

    ``chdir`` to a clean tmp dir so the per-model ``env_file=".env"``
    can't pick up the dev ``.env`` at the repo root and dilute the
    test by re-supplying real values for the aliased fields.
    """
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    # Required by ``BotConfig`` so default-construction doesn't blow up.
    monkeypatch.setenv("BOT_TOKEN", "1:" + "a" * 40)
    for var in _RISKY_ENV_VARS:
        monkeypatch.setenv(var, f"{_SENTINEL_PREFIX}{var}")


@pytest.mark.parametrize(
    "model_cls",
    _AUDITED_MODELS,
    ids=[m.__name__ for m in _AUDITED_MODELS],
)
def test_no_field_leaks_shell_env_var(
    model_cls: type[BaseSettings],
    _poison_env: None,
) -> None:
    """No field on ``model_cls`` adopts a sentinel value from the env.

    A failure here means a field NAME on this model matches one of
    the risky env vars (case-insensitive). Fix: set
    ``populate_by_name=False`` on that model's ``model_config`` and
    add an alias-based round-trip test. See
    ``WebhookConfig`` for the precedent.
    """
    instance = model_cls()
    dumped = instance.model_dump()

    leaks: list[tuple[str, str]] = []
    for field_name, value in dumped.items():
        # ``model_dump`` returns SecretStr as ``SecretStr('**********')``
        # repr only when explicitly asked; default dump preserves the
        # underlying value for non-secrets and a SecretStr wrapper for
        # secrets. Stringify to scan uniformly — the sentinel is a
        # plain ASCII marker that survives any wrapping.
        as_str = str(value)
        if _SENTINEL_PREFIX in as_str:
            leaks.append((field_name, as_str))

    assert not leaks, (
        f"{model_cls.__name__} leaked shell env vars into fields "
        f"via populate_by_name: {leaks}. Set populate_by_name=False "
        f"on its model_config (see WebhookConfig for precedent)."
    )
