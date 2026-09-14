"""Stage 0 smoke test — package imports cleanly."""

from __future__ import annotations

import telegram_invite_bot


def test_package_version_present() -> None:
    assert telegram_invite_bot.__version__
