"""Sentry initialiser.

Wired from :func:`app.build_app` immediately after logging is
configured so any subsequent loguru exception line also lands as a
Sentry event. Loud no-op when ``SENTRY_DSN`` is unset — dev runs
shouldn't be blocked by missing observability config. The no-op was
originally justified as parity with a legacy ``main.py`` that didn't
initialise Sentry either; that file is gone (T-011), and what the
posture now protects is the operator's ``.env``, where the DSN has
never been mandatory and adding a hard requirement would turn a
missing optional into a failed boot.

Why a dedicated module instead of an inline ``sentry_sdk.init`` in
``app.py``?

* Keeps the bootstrap file readable — Sentry's options surface has
  10+ knobs (release, environment, traces_sample_rate, integrations,
  in_app_include, …) and they don't belong next to the dishka
  container.
* Lets tests pin the init contract without bringing up the whole
  application graph: instantiate a ``Settings``, call
  :func:`init_sentry`, assert the resulting ``sentry_sdk.init`` kwargs.
* Lets us flip the integration set (e.g. add AsyncioIntegration when
  it stabilises) in one place behind a single docstring.

The function returns ``True`` when init actually ran. Callers can
log a one-liner so operators can confirm the wiring from logs alone
without grepping for the Sentry SDK's own startup line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sentry_sdk
from loguru import logger
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.loguru import LoguruIntegration

from telegram_invite_bot import __version__

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="observability.sentry")


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry from typed settings.

    Returns
    -------
    True if ``sentry_sdk.init`` was called (DSN configured), False
    otherwise. The boolean shape lets the caller emit a single
    "Sentry on/off" line at startup; we don't surface the SDK's
    internal state.

    Side effects
    ------------
    Calls :func:`sentry_sdk.init` (process-global). Idempotent in
    practice — the SDK swaps the hub atomically, so calling it twice
    is safe but wasteful. Tests that need clean state should use the
    ``monkeypatch`` fixture rather than relying on per-test SDK reset.
    """
    dsn = settings.observability.sentry_dsn
    if dsn is None or not dsn.get_secret_value().strip():
        # Loud-enough log for ops to confirm absence is intentional:
        # a silent "Sentry not initialised" is a known foot-shoot when
        # an operator typo'd the env var name and assumed it was on.
        log.info("SENTRY_DSN not set; error reporting is OFF")
        return False

    sentry_sdk.init(
        dsn=dsn.get_secret_value(),
        # ``release`` lets Sentry group regressions by deploy; we use
        # the package version (kept in lockstep with pyproject) rather
        # than git SHA so a tagged release maps to a release in Sentry
        # without a CI step.
        release=f"telegram-invite-bot@{__version__}",
        # ``environment`` separates dev/staging/prod alerts. Without
        # this, dev exceptions would page the on-call rotation that
        # filters on prod-only.
        environment=settings.app_env.value,
        # Performance: tiny default so a healthy prod doesn't burn
        # quota on traces nobody reads. 0.0 = events only. Raise
        # ``SENTRY_TRACES_SAMPLE_RATE`` in an incident to capture more
        # spans without a deploy — #1995 made that env var real; until
        # then this line named it while passing a hardcoded 0.0, so
        # the one instruction here that is meant to be followed under
        # pressure was the one that did nothing.
        traces_sample_rate=settings.observability.sentry_traces_sample_rate,
        # Integrations:
        # * LoguruIntegration → every loguru.error/exception lands in
        #   Sentry as an event with the bound ``component`` tag as a
        #   "logger" attribute. Without it, our error handler's
        #   ``log.exception(...)`` line would Sentry-silent.
        # * AsyncioIntegration → tags concurrent tasks with their
        #   coroutine name so an async traceback in a middleware
        #   doesn't get reported as "<unknown>".
        integrations=[LoguruIntegration(), AsyncioIntegration()],
        # ``send_default_pii=False`` is the SDK default but we set it
        # explicitly: Telegram updates carry user IDs, names, message
        # text — never auto-forward any of that. The error handler
        # decides what to attach via ``set_context``.
        send_default_pii=False,
        # ``include_local_variables`` defaults to True, and that
        # default is wrong here. Sentry attaches every frame's locals
        # to a reported traceback and scrubs them by *name* against a
        # denylist — "password", "secret", "token", "api_key" and a
        # handful more. Our comparison sites do not use those names:
        # ``webhook/security.py`` holds the Telegram secret token in
        # ``expected_value`` and the guide editor holds
        # ``GUIDES_EDIT_SECRET`` in ``expected``, both of them plain
        # ``str`` by the time they reach ``compare_digest``. An
        # exception raised anywhere below those frames would ship the
        # cleartext to a third party, and the surface is exactly the
        # code we are most careful about everywhere else.
        #
        # Renaming the two locals onto the denylist would fix those two
        # and nothing else — the property we want is that no frame's
        # locals leave the process, whatever a future one is called.
        # The cost is real and accepted: a traceback without locals is
        # harder to read. ``set_context`` in the error handler is where
        # the chosen, non-secret detail goes instead.
        include_local_variables=False,
        # ``before_send`` is a hook we'll grow when the first false
        # positive shows up (e.g. dropping TelegramForbiddenError —
        # user blocked the bot, not an actionable alert). Leaving as
        # default ``None`` for now keeps the surface honest.
    )
    log.info(
        "Sentry initialised: env={env} release={release}",
        env=settings.app_env.value,
        release=__version__,
    )
    return True
