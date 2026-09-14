"""``/admin_crypto`` — kernel crypto algorithms from /proc/crypto.

The existing crypto surface (/admin_hashlib, /admin_ssl,
/admin_certfp, /admin_codecs) explains **userspace**
cryptography — OpenSSL build, hashlib backends, certificate
fingerprints. None of them touch the **kernel-side** algorithm
registry. That matters because:

* **dm-crypt / fscrypt rely on kernel crypto.** When LUKS is slow
  or fscrypt is unavailable, the diagnosis is almost always that
  the AES driver is the generic software fallback rather than
  the hardware-accelerated AES-NI / ARMv8-CE / Power VMX variant.
  /proc/crypto names the driver per algorithm — generic-suffix
  = software, anything-else = accelerated.
* **TLS in-kernel (kTLS).** The ``ktls`` socket option requires
  ``gcm(aes)``, ``ccm(aes)`` etc. to be registered. A missing
  algorithm explains a silent fallback to userspace TLS.
* **VPN (IPsec / WireGuard / OpenVPN).** Throughput problems on
  these stacks usually trace back to crypto driver choice.
* **Self-test failures.** Each registered algorithm carries a
  ``selftest`` line. Hardware drivers that fail self-test mean
  the kernel falls back to software silently — and operator
  performance assumptions are wrong. This is the one place we
  surface that signal explicitly.

The format is a sequence of key:value blocks separated by blank
lines. Each block describes one algorithm registration with at
least ``name``, ``driver``, ``module``, ``type``, ``selftest``
keys (a few algorithm-specific extras like ``blocksize``,
``digestsize``, ``ivsize``). Format has been stable since the
modern crypto API landed (~2.6.19, 2007).

Curation: too many algorithms to render verbatim (a modern Linux
host registers 300+). We aggregate by ``type`` (skcipher, shash,
ahash, aead, akcipher, kpp, …) and surface:

* Total algorithm count.
* Per-type counts.
* The set of algorithms whose self-test FAILED — the single ⚠
  predicate. A failed self-test is unambiguously bad: the kernel
  refuses to use that driver and falls back silently.

Forward-compat: parser is purely key:value driven. Unknown keys
are ignored (we only consume the ones we render). Unknown types
bucket into "other" — adding a type to the registry doesn't
break the card.

⚠ predicate: single. Failed self-test count > 0. Pinned with
explicit must-not-fire test on the canonical (all-passed) sample.

Same wiring as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only path injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.crypto")


_CRYPTO_PATH = Path("/proc/crypto")


class _CryptoAlgo:
    """One registered crypto algorithm.

    Fields are the subset we surface; unknown keys are silently
    dropped at parse time. ``selftest_passed`` is precomputed
    from the ``selftest`` field's literal text — the kernel emits
    ``passed`` / ``failed`` / ``unknown``.
    """

    __slots__ = (
        "driver",
        "module",
        "name",
        "selftest_passed",
        "selftest_raw",
        "type",
    )

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        module: str,
        type: str,
        selftest_raw: str,
    ) -> None:
        self.name = name
        self.driver = driver
        self.module = module
        self.type = type
        self.selftest_raw = selftest_raw
        self.selftest_passed = selftest_raw == "passed"


class _CryptoSnapshot:
    """Captured /proc/crypto.

    ``algos`` — every parsed algorithm.
    ``available`` — False on macOS / non-procfs (very rarely
    Linux-without-crypto, an embedded build).
    """

    __slots__ = ("algos", "available")

    def __init__(self, *, algos: tuple[_CryptoAlgo, ...], available: bool) -> None:
        self.algos = algos
        self.available = available

    @property
    def failed_selftest_count(self) -> int:
        """Algorithms whose selftest_raw is anything other than
        'passed'. We count 'failed' AND 'unknown' here because
        'unknown' on a production kernel almost always means the
        test never ran — same operator response (investigate),
        same surfacing decision."""
        return sum(1 for a in self.algos if not a.selftest_passed)


def _parse_crypto(text: str) -> tuple[_CryptoAlgo, ...]:
    """Parse /proc/crypto's blank-line-separated key:value blocks.

    Each block becomes a dict; we extract the keys we care about
    and ignore the rest. A block missing any required key is
    dropped (defensive — kernel always emits the required set,
    but a partial/corrupt read shouldn't crash).
    """
    algos: list[_CryptoAlgo] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if not block:
            return
        required = ("name", "driver", "module", "type", "selftest")
        if all(k in block for k in required):
            algos.append(
                _CryptoAlgo(
                    name=block["name"],
                    driver=block["driver"],
                    module=block["module"],
                    type=block["type"],
                    selftest_raw=block["selftest"],
                )
            )
        block.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush()
            continue
        # Block lines are `key : value` with colon separator.
        # Kernel pads the key column for alignment; we strip both
        # sides.
        if ":" not in line:
            # Not a key:value line — skip defensively.
            continue
        key, _, value = line.partition(":")
        block[key.strip()] = value.strip()
    # Trailing block without blank line terminator.
    flush()
    return tuple(algos)


def _capture(*, path: Path = _CRYPTO_PATH) -> _CryptoSnapshot:
    """Read /proc/crypto + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _CryptoSnapshot(algos=(), available=False)
    return _CryptoSnapshot(algos=_parse_crypto(text), available=True)


def _render(snap: _CryptoSnapshot) -> str:
    lines = ["🔏 <b>Kernel crypto algorithms (/proc/crypto)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/crypto unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.algos:
        lines.append(
            "  <i>No crypto algorithms registered — embedded kernel "
            "or extremely stripped build. dm-crypt / fscrypt / kTLS "
            "would all be non-functional.</i>"
        )
        return "\n".join(lines)

    # Aggregate by type.
    by_type: dict[str, int] = {}
    for a in snap.algos:
        by_type[a.type] = by_type.get(a.type, 0) + 1

    failed = [a for a in snap.algos if not a.selftest_passed]
    failed_marker = " ⚠" if failed else ""

    lines.append(
        f"  <b>Algorithms registered:</b> "
        f"<code>{len(snap.algos)}</code>  "
        f"<b>failed self-test:</b> <code>{len(failed)}</code>"
        f"{failed_marker}"
    )
    lines.append("")
    lines.append("  <b>By type:</b>")
    # Sort types by count desc — operator usually scans for the
    # heavy hitters (skcipher, shash) first.
    for type_name, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"    • <code>{type_name}</code>: <code>{count}</code>")

    if failed:
        lines.append("")
        lines.append("  <b>Failed self-test:</b>")
        # Cap defensively; in practice the failed list is tiny.
        for algo in failed[:20]:
            lines.append(
                f"    • <code>{algo.name}</code> "
                f"(driver <code>{algo.driver}</code>, "
                f"selftest=<code>{algo.selftest_raw}</code>)"
            )
        if len(failed) > 20:
            lines.append(f"    <i>… {len(failed) - 20} more.</i>")

    lines.append("")
    if failed:
        lines.append(
            "<i>⚠ Self-test failure means the kernel REGISTERED the "
            "driver but refuses to dispatch crypto operations to it — "
            "it falls back to a different implementation silently. "
            "Performance assumptions about hardware acceleration are "
            "wrong while any of these are in the list. Most common "
            "cause: a hardware driver loaded on the wrong CPU "
            "feature-set, or a bisect-broken kernel build.</i>"
        )
    else:
        lines.append(
            "<i>No warnings beyond the self-test predicate by design — "
            "the choice between e.g. AES-NI accelerated and software "
            "fallback is a hardware-feature question, not a problem "
            "signal. Inspect the <code>driver</code> column of a "
            "specific algorithm via direct /proc/crypto read if a "
            "throughput investigation needs it.</i>"
        )
    return "\n".join(lines)


async def handle_admin_crypto(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_crypto; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        algo_count=len(snap.algos),
        failed_selftest=snap.failed_selftest_count,
    ).info("/admin_crypto rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.crypto")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_crypto(message, settings)

    router.message.register(_entry, Command("admin_crypto", ignore_case=True))
    return router
