"""``/admin_*`` cards build their own markup — nothing escapes it for them.

The i18n catalogue is guarded by ``test_i18n_html_safety.py``, but the
~110 diagnostic cards under ``handlers/admin/`` never touch it: each
one f-strings machine-derived values straight into ``<code>…</code>``
and hands the result to ``message.answer`` under the bot-wide
``parse_mode=HTML`` (``di/providers.py``). Telegram then refuses the
whole message and the developer gets silence instead of a card.

Four of them were doing exactly that, and not rarely:

* ``/admin_warnings`` — ``_describe_pattern`` renders an unrestricted
  filter as ``<any>``. CPython's *default* filter list is seven such
  entries, so the card 400'd on every invocation, always.
* ``/admin_pythonpath`` — ``<unset>`` for an absent ``PYTHONPATH``
  (which is how systemd runs the unit) and ``<cwd>`` for the implicit
  path entry.
* ``/admin_locale`` — ``<unset>`` for an absent ``LANG``, same reason.
* ``/admin_tasks`` — a task whose coroutine is a nested function has
  ``<locals>`` in its ``__qualname__``; the ``repr`` fallback and the
  ``<done>`` sentinel carry angles too.

``bot_session.py`` had already hand-written ``&lt;unset&gt;`` in its
two literal branches, which is what makes this an oversight rather
than a convention: the rule was known, just not applied where the
value arrived as data.

The fix is ``html.escape`` at the render boundary, so the operator
still reads ``<any>`` — the sentinel is informative and worth keeping.
That is asserted below too: escaping must not be "quietly drop the
angle brackets", or the card would start claiming an unrestricted
filter matches the empty pattern.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import re
import signal
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings

from telegram_invite_bot.handlers import admin as admin_pkg
from tests.telegram_html import telegram_html_errors

pytestmark = pytest.mark.integration

#: Below this the sweep has stopped driving anything and its green is
#: meaningless. macOS drives 70: the /proc-backed cards still render,
#: they just render their "file not present" branch. Linux drives the
#: same set with populated data. 60 leaves headroom for platform
#: variance without letting the sweep quietly collapse to a handful.
_MIN_SWEPT = 60


def _sweep() -> tuple[dict[str, list[str]], list[str]]:
    """Render every admin card that can be driven with no fixtures.

    Deliberately duck-typed on the ``_capture()`` / ``_render(rows)``
    pair rather than a hand-maintained list: a new card added next
    month is covered the day it lands, and a card that grows a
    parameter drops out loudly via ``_MIN_SWEPT`` rather than
    silently passing.

    Broad ``except Exception`` is the point, not laziness — a card
    reading ``/proc/net/tcp`` raises ``FileNotFoundError`` on macOS
    and ``PermissionError`` in some containers. Neither is a finding;
    both would otherwise turn this into a platform test.
    """
    offenders: dict[str, list[str]] = {}
    swept: list[str] = []
    for mod in pkgutil.iter_modules(admin_pkg.__path__):
        try:
            module = importlib.import_module(f"{admin_pkg.__name__}.{mod.name}")
        except Exception:  # noqa: BLE001 — see docstring
            continue
        capture = getattr(module, "_capture", None)
        render = getattr(module, "_render", None)
        if not (callable(capture) and callable(render)):
            continue
        try:
            # Only a parameter WITHOUT a default makes a card
            # undrivable. Most ``_capture`` signatures take
            # ``path: Path = Path("/proc/…")`` purely so tests can
            # point them at a fixture; refusing those excluded ~50 of
            # the ~110 cards from the sweep for no reason at all.
            if any(
                param.default is inspect.Parameter.empty
                for param in inspect.signature(capture).parameters.values()
            ):
                continue
            rows = capture()
            if inspect.iscoroutine(rows):
                # An async ``_capture`` needs a session/bot we do not
                # have here. Close it so the skip does not emit a
                # "coroutine was never awaited" warning — which this
                # suite turns into an error.
                rows.close()
                continue
            if len(inspect.signature(render).parameters) != 1:
                continue
            rendered = render(rows)
        except Exception:  # noqa: BLE001 — see docstring
            continue
        swept.append(mod.name)
        errors = telegram_html_errors(rendered)
        if errors:
            offenders[mod.name] = errors
    return offenders, swept


def test_every_drivable_admin_card_renders_valid_telegram_html() -> None:
    offenders, swept = _sweep()
    assert not offenders, (
        "these /admin_* cards render markup Telegram refuses to parse — "
        f"the send 400s and the developer gets nothing:\n{offenders}"
    )
    assert len(swept) >= _MIN_SWEPT, (
        f"the sweep only drove {len(swept)} cards ({swept}) — below "
        f"{_MIN_SWEPT} it is not evidence of anything"
    )


def test_admin_warnings_survives_an_unrestricted_filter() -> None:
    """The always-on case: CPython ships default filters with no
    message pattern, which ``_describe_pattern`` renders ``<any>``."""
    from telegram_invite_bot.handlers.admin import warnings_view as mod

    assert mod._describe_pattern(re.compile("")) == "<any>"
    row = mod._FilterRow(
        action="ignore",
        message=mod._describe_pattern(re.compile("")),
        category="<unknown>",
        module=mod._describe_pattern(re.compile("")),
        lineno=0,
    )
    rendered = mod._render([row])
    assert not telegram_html_errors(rendered), rendered
    # Escaping, not deletion: an operator has to keep seeing that the
    # filter is unrestricted.
    assert "&lt;any&gt;" in rendered, rendered


def test_admin_pythonpath_survives_the_cwd_and_unset_sentinels() -> None:
    from telegram_invite_bot.handlers.admin import pythonpath as mod

    assert mod._classify("") == "<cwd>"
    rendered = mod._render([mod._PathEntry(index=0, path="", kind="<cwd>")], "<unset>")
    assert not telegram_html_errors(rendered), rendered
    assert "&lt;cwd&gt;" in rendered and "&lt;unset&gt;" in rendered, rendered
    # The ⚠ marker keys off the raw kind, so escaping must not have
    # been pushed back into ``_classify``.
    assert "shadowing vector" in rendered, rendered


def test_admin_locale_survives_an_unset_lang() -> None:
    from telegram_invite_bot.handlers.admin import locale_info as mod

    snap = mod._LocaleSnapshot(
        lc_all="<unset>",
        preferred="UTF-8",
        fs_encoding="utf-8",
        stdout_enc="<binary>",
        stderr_enc="utf-8",
        lang_env="<unset>",
    )
    rendered = mod._render(snap)
    assert not telegram_html_errors(rendered), rendered
    assert "&lt;unset&gt;" in rendered and "&lt;binary&gt;" in rendered, rendered


def test_admin_tasks_survives_a_nested_function_coroutine() -> None:
    """``<locals>`` is in the qualname of every closure-based task —
    the shape that turns a leak-detection card into a 400 on exactly
    the busy loop it exists to inspect."""
    from telegram_invite_bot.handlers.admin import tasks as mod

    rows = [
        mod._TaskRow(name="Task-7", coro="build_router.<locals>._entry", done=False),
        mod._TaskRow(name="probe<x>", coro="<done>", done=True),
    ]
    rendered = mod._render(rows)
    assert not telegram_html_errors(rendered), rendered
    assert "&lt;locals&gt;" in rendered and "&lt;done&gt;" in rendered, rendered


def test_admin_tasks_escapes_a_live_snapshot() -> None:
    """End-to-end on a real loop, not a hand-built row list: proves
    ``_sample_tasks`` really does hand ``_render`` an angle-bearing
    qualname, so the row-level test above is not testing a shape the
    production path never produces."""
    from telegram_invite_bot.handlers.admin import tasks as mod

    async def drive() -> tuple[list[mod._TaskRow], str]:
        async def _nested() -> None:  # qualname carries "<locals>"
            await asyncio.sleep(5)

        task = asyncio.create_task(_nested(), name="probe-sleeper")
        try:
            rows = mod._sample_tasks()
            return rows, mod._render(rows)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    rows, rendered = asyncio.run(drive())
    assert any("<locals>" in r.coro for r in rows), [r.coro for r in rows]
    assert not telegram_html_errors(rendered), rendered


def test_admin_signals_survives_a_repr_fallback_handler() -> None:
    """``repr`` of a C-level callable is ``<built-in function …>``."""
    from telegram_invite_bot.handlers.admin import signals as mod

    desc, _, _ = mod._describe_handler(signal.default_int_handler)
    row = mod._SignalRow(
        name="SIGINT",
        handler_desc=f"{desc} / {object()!r}",
        is_default=False,
        is_ignored=False,
    )
    rendered = mod._render([row])
    assert not telegram_html_errors(rendered), rendered


def test_admin_status_survives_an_unset_webhook_url() -> None:
    """``/admin_status`` is invisible to the sweep — its renderer is
    ``_render_status`` and it needs a ``Settings`` — so it gets its own
    pin. ``_redact_webhook_url`` returns ``<unset>`` for an empty URL
    and ``<malformed>`` for one without a scheme; both used to land in
    ``<code>…</code>`` raw, which 400s the whole card.

    ``build=None`` is the missing-``BUILD_INFO`` branch — the one a box
    gets when it was deployed by anything other than
    ``scripts/deploy.sh``, i.e. the common one rather than the exotic
    one.
    """
    from telegram_invite_bot.handlers.admin import status as mod

    settings = cast(
        "Settings",
        SimpleNamespace(
            app_env=SimpleNamespace(value="test"),
            webhook=SimpleNamespace(url=""),
            throttling=SimpleNamespace(enabled=False, capacity=0, refill_per_second=0),
        ),
    )
    rendered = mod._render_status(settings, db_results={"main": True}, sentry_on=False, build=None)
    assert not telegram_html_errors(rendered), rendered
    # Escaped, not deleted: the operator still learns the URL is unset.
    assert "&lt;unset&gt;" in rendered, rendered

    malformed = cast(
        "Settings",
        SimpleNamespace(
            app_env=SimpleNamespace(value="test"),
            webhook=SimpleNamespace(url="not-a-url"),
            throttling=SimpleNamespace(enabled=False, capacity=0, refill_per_second=0),
        ),
    )
    rendered = mod._render_status(malformed, db_results={}, sentry_on=False, build=None)
    assert not telegram_html_errors(rendered), rendered
    assert "&lt;malformed&gt;" in rendered, rendered


def test_the_sweep_would_notice_a_regression() -> None:
    """Guard the guard: the sweep's green has to be falsifiable.

    Re-renders one real card with the escaping removed the way the
    pre-fix code had it, and requires the validator to reject it. If
    this ever passes, the sweep above is asserting nothing.
    """
    from telegram_invite_bot.handlers.admin import warnings_view as mod

    unescaped = f"⚠️ <b>Warning filters</b>\n• <code>{mod._describe_pattern(re.compile(''))}</code>"
    assert telegram_html_errors(unescaped), unescaped


def test_admin_help_survives_an_argument_placeholder() -> None:
    """``/admin_help`` is invisible to the sweep, so it gets its own pin.

    Its renderer is ``_render_pages`` and it needs a ``Settings``, which
    is exactly why the sweep above never drove it — the same reason
    ``/admin_status`` has a pin of its own two tests up.

    The catalog documents a command's argument the way every CLI does,
    ``/set_crypto_token <token>``, and that string went into
    ``<code>…</code>`` raw. Telegram read ``<token>`` as a start tag it
    does not know and refused the page whole; prod's journal carries
    ``Unsupported start tag "token"`` for it. The card is the index of
    the whole ~110-command admin surface, so the developer who typed
    ``/admin_help`` to find a command got silence instead.
    """
    from telegram_invite_bot.handlers.admin import help as mod

    settings = cast("Settings", SimpleNamespace(bot=SimpleNamespace(developer_ids={555})))
    pages = mod._render_pages(settings)
    assert pages
    for page in pages:
        assert not telegram_html_errors(page), page
    # Escaped, not deleted: an index that hides the argument stops
    # being an index. Same posture as the ``<any>`` sentinel above.
    assert "&lt;token&gt;" in "\n".join(pages), pages


def test_admin_panel_escapes_a_command_argument_placeholder() -> None:
    """The panel renders the same catalog shape and was one line away.

    Sweeping the real sections proves only that today's data is clean —
    the panel lists ``/set_crypto_token`` bare, without its argument.
    The synthetic section is the half that can actually fail: it is the
    entry the next contributor writes when they document an argument,
    and unescaped it takes the whole category screen down rather than
    one line of it.
    """
    from telegram_invite_bot.handlers.admin import panel as mod

    for lang in ("ru", "en"):
        for section in mod._SECTIONS:
            rendered = mod._render_section(section, lang)
            assert not telegram_html_errors(rendered), (section[0], lang, rendered)

    synthetic: mod._Section = (
        "probe",
        ("probe", "probe"),
        ("🧪 <b>Probe</b>", "🧪 <b>Probe</b>"),
        (("/set_crypto_token <token>", ("store the token", "store the token")),),
    )
    rendered = mod._render_section(synthetic, "ru")
    assert not telegram_html_errors(rendered), rendered
    assert "&lt;token&gt;" in rendered, rendered


def test_admin_help_escapes_the_description_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the line, which today's data cannot falsify.

    Every description in the catalog is plain prose right now, so
    escaping ``desc`` is unfalsifiable against the real constant — and
    an unfalsifiable half is how the ``cmd`` half shipped broken in the
    first place. A description is the natural place to write ``<none>``
    or ``value <= N``, and it lands in the same message, so it takes the
    same page down.
    """
    from telegram_invite_bot.handlers.admin import help as mod

    monkeypatch.setattr(
        mod,
        "_ADMIN_COMMANDS",
        (("/admin_probe", "prints <none> when the source is absent"),),
    )
    settings = cast("Settings", SimpleNamespace(bot=SimpleNamespace(developer_ids=set())))
    pages = mod._render_pages(settings)
    joined = "\n".join(pages)
    assert not telegram_html_errors(joined), joined
    assert "&lt;none&gt;" in joined, joined
