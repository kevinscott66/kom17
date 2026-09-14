"""Process-wide observability bootstrap.

Right now holds the Sentry initialiser. Prometheus counters live in
:mod:`telegram_invite_bot.webhook.metrics` next to the HTTP surface
that exposes them; logging is wired in :mod:`config.logging`. This
package is the home for anything cross-cutting that doesn't fit
either spot — tracing, error reporting, custom span exporters.
"""

from __future__ import annotations

from telegram_invite_bot.observability.sentry import init_sentry

__all__ = ["init_sentry"]
