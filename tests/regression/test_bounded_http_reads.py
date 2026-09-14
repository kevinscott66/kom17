"""Regression guard: no outbound HTTP read without a size ceiling (#119).

``await client.get(url)`` buys the whole response body into memory, and
httpx bounds it with a *read timeout* only — a peer answering ``200`` and
then trickling gigabytes stays inside every per-chunk deadline the entire
way down. One process serves every chat here, so the OOM killer taking
it is a full outage caused by a single upstream.

:func:`telegram_invite_bot.utils.http_read.send_capped` is the sanctioned
call shape. This guard exists because the defect is invisible at review
time: a new integration written the obvious way looks exactly like
correct code, and only misbehaves against an upstream nobody controls.

Scope
-----
Verb calls (``get``/``post``/``put``/``patch``/``delete``/``head``/
``request``/``stream``) on a receiver that names an httpx client. The
receiver test is what keeps ``repo.get(...)``, ``session.get(...)`` and
``dict.get(...)`` out of the sweep.

Waiver: ``# http-cap: allow (<reason>)`` on the call's own source lines,
matching the ``# money-guard: allow`` convention already in the tree. The
one shape that legitimately needs it is a deliberately streamed read that
bounds itself — ``send_capped`` is built out of exactly that.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"

_HTTP_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)

#: Substrings that mark a receiver as an httpx client. Deliberately
#: narrow: ``client``/``session`` alone would sweep in the DB session and
#: the Telegram bot session, neither of which reads an unbounded body.
_HTTPX_RECEIVER_HINTS = ("client", "httpx")

#: Receivers that contain a hint substring but are NOT httpx clients.
_RECEIVER_EXCLUSIONS = ("bot", "storage", "redis", "engine", "session_for")

_WAIVER = "# http-cap: allow"


def _is_httpx_receiver(node: ast.expr) -> bool:
    text = ast.unparse(node).lower()
    if any(bad in text for bad in _RECEIVER_EXCLUSIONS):
        return False
    return any(hint in text for hint in _HTTPX_RECEIVER_HINTS)


def _offending_calls(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_VERBS:
            continue
        if not _is_httpx_receiver(func.value):
            continue
        # The waiver may sit on any line the call spans — a multi-line
        # call is the common shape and there is no single "own" line.
        end = node.end_lineno or node.lineno
        span = "\n".join(lines[node.lineno - 1 : end])
        if _WAIVER in span:
            continue
        found.append((node.lineno, ast.unparse(func)))
    return found


def test_no_unbounded_httpx_reads_outside_the_helper() -> None:
    """Every third-party HTTP read goes through ``send_capped``.

    ``http_read.py`` is the one file allowed to call the client directly:
    it is where the streaming and the ceiling live. Anything else that
    trips this has reintroduced #119 — route it through ``send_capped``,
    or add ``# http-cap: allow (<why>)`` if the call really does bound
    its own read.
    """
    helper = SRC_ROOT / "utils" / "http_read.py"
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == helper:
            continue
        for lineno, call in _offending_calls(path):
            offenders.append(f"{path.relative_to(SRC_ROOT)}:{lineno}: {call}(...)")

    assert not offenders, "unbounded HTTP reads:\n" + "\n".join(offenders)


def test_the_guard_actually_sees_a_plain_client_call(tmp_path: Path) -> None:
    """A guard that matches nothing passes forever. This pins that the
    receiver/verb detection still fires on the exact shape #119 removed
    from the nine call sites."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "async def f(client):\n"
        "    a = await client.get('https://x.test')\n"
        "    b = await client.post('https://x.test', json={})\n"
        "    c = await client.get('https://x.test')  # http-cap: allow (test)\n"
        "    d = repo.get(1)\n"
        "    return a, b, c, d\n",
        encoding="utf-8",
    )
    assert [lineno for lineno, _ in _offending_calls(probe)] == [2, 3]
