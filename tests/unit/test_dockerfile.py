"""Structural invariants on the Dockerfile and docker-compose.yml.

We don't run ``docker build`` here — that requires a daemon and adds
minutes to CI. Instead we pin the load-bearing claims the files make
about themselves:

* multi-stage build (final image has no compilers)
* runs as non-root
* exposes the port Settings advertises
* healthcheck targets ``/healthz`` (the route our FastAPI exposes)
* tini-as-PID-1 so SIGTERM reaches uvicorn
* compose mounts ``database/`` and ``logs/`` (state survives ``down``)
* compose security knobs are set (read-only FS, no-new-privileges)

If somebody deletes the non-root user or moves ``EXPOSE`` to a port
nginx isn't proxying, this test catches the regression before deploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return (_ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_doc() -> dict:
    raw = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return yaml.safe_load(raw)


def test_dockerfile_is_multi_stage(dockerfile_text: str) -> None:
    """Final image MUST start FROM a fresh base — no build-time
    toolchain bleeding into runtime. A single-stage build would ship
    gcc, headers, the pip cache, and ~250 MB of apt cruft.
    """
    from_lines = [line for line in dockerfile_text.splitlines() if line.strip().startswith("FROM ")]
    assert len(from_lines) >= 2, "Dockerfile must be multi-stage"
    # And the final FROM must NOT be the builder — i.e. the runtime
    # stage starts from a clean base.
    assert " AS runtime" in from_lines[-1], "last FROM must be the runtime stage"


def test_dockerfile_runs_as_nonroot(dockerfile_text: str) -> None:
    # ``USER app`` (or a numeric UID that isn't 0) must appear AFTER
    # the last FROM. A root-only image is a regulatory red flag and
    # makes any container-escape CVE much more useful to an attacker.
    runtime_section = dockerfile_text.rsplit("AS runtime", 1)[-1]
    assert "USER app" in runtime_section, "runtime stage must drop to non-root"


def test_dockerfile_exposes_webhook_port(dockerfile_text: str) -> None:
    # 8080 is the Settings default (``WebhookConfig.port``). If we
    # bump that default this test will fail in lockstep — the right
    # place to fix is BOTH files, not just one.
    from telegram_invite_bot.config.settings import WebhookConfig

    default_port = WebhookConfig().port
    assert f"EXPOSE {default_port}" in dockerfile_text, (
        f"Dockerfile must EXPOSE the Settings default port ({default_port})"
    )


def test_dockerfile_binds_all_interfaces_explicitly(dockerfile_text: str) -> None:
    """The image must say ``HOST=0.0.0.0`` out loud.

    The application defaults to loopback, which is the right default on
    a bare host behind nginx and fatally wrong inside a container: a
    socket bound to 127.0.0.1 in the container's net namespace cannot
    be reached through the published port, so the container comes up,
    passes nothing that looks at it from outside, and serves zero
    traffic. Compose sets ``HOST: ${HOST:-0.0.0.0}`` for the same
    reason, but an image run with plain ``docker run`` never reads
    compose — the Dockerfile is where the invariant has to live.
    """
    from telegram_invite_bot.config.settings import WebhookConfig

    assert WebhookConfig().host == "127.0.0.1", (
        "this test exists because the application default is loopback; "
        "if that changed, revisit whether the image still needs the override"
    )
    runtime_section = dockerfile_text.rsplit("AS runtime", 1)[-1]
    assert "ENV HOST=0.0.0.0" in runtime_section, (  # noqa: S104 — inside a container, see above
        "runtime stage must bind all interfaces explicitly"
    )


def test_dockerfile_healthcheck_hits_healthz(dockerfile_text: str) -> None:
    """The FastAPI app exposes ``/healthz`` — a pure liveness probe that
    answers 200 whenever the event loop answers and never touches a
    database (the engine probe lives on ``/readyz``). Any other path
    returns 404 and Docker would mark the container unhealthy on every
    check.
    """
    assert "HEALTHCHECK" in dockerfile_text
    assert "/healthz" in dockerfile_text


def test_dockerfile_uses_tini_for_signal_forwarding(dockerfile_text: str) -> None:
    """Without tini (or equivalent PID-1), SIGTERM never reaches the
    uvicorn process and ``docker stop`` SIGKILLs after the grace
    period — any in-flight webhook update is lost. Required for the
    blue/green cutover where we drain before swap.
    """
    assert "tini" in dockerfile_text
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in dockerfile_text


def test_compose_mounts_database_and_logs(compose_doc: dict) -> None:
    """Bind mounts (not named volumes) — host-side files survive
    ``docker compose down`` and the legacy .db files at the repo root
    are immediately visible to the container with zero copy step.
    """
    volumes = compose_doc["services"]["bot"]["volumes"]
    targets = {v.split(":")[1] for v in volumes if isinstance(v, str)}
    assert "/app/database" in targets
    assert "/app/logs" in targets


def test_compose_locks_down_runtime(compose_doc: dict) -> None:
    """Defense in depth — an RCE in any dep shouldn't be able to
    write arbitrary files or escalate via setuid binaries.
    """
    svc = compose_doc["services"]["bot"]
    assert svc.get("read_only") is True, "compose must enable read-only root FS"
    sec_opts = svc.get("security_opt", [])
    assert "no-new-privileges:true" in sec_opts, (
        "compose must set no-new-privileges to block setuid escalation"
    )


def test_compose_publishes_settings_default_port(compose_doc: dict) -> None:
    from telegram_invite_bot.config.settings import WebhookConfig

    default_port = WebhookConfig().port
    ports = compose_doc["services"]["bot"]["ports"]
    # Default-substitution in compose: ``"${PORT:-8080}:8080"``. We
    # parse the rhs (container side) — that's the actual contract
    # with the Dockerfile's EXPOSE.
    container_ports = [str(p).split(":")[-1] for p in ports]
    assert str(default_port) in container_ports


def test_dockerignore_excludes_local_dbs_and_envs() -> None:
    """``database/`` and ``.env`` MUST NOT be baked into the image —
    one would ship the dev's user data, the other would leak the bot
    token. The .dockerignore is the single line of defense here.
    """
    ignored = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = {
        line.strip() for line in ignored if line.strip() and not line.strip().startswith("#")
    }
    assert "database/" in patterns
    assert ".env" in patterns
    assert "*.db" in patterns


def test_runtime_image_can_import_the_package(dockerfile_text: str) -> None:
    """The venv the runtime stage inherits must carry the code itself.

    The builder installs into ``/opt/venv`` and the runtime stage copies
    that directory across; everything else about ``/build`` is thrown
    away with the stage. An *editable* install of the package therefore
    cannot survive the crossing: hatchling writes it as a ``.pth`` file
    holding the single absolute path ``/build/src``, and that path does
    not exist in the runtime image. ``site.py`` drops a ``.pth`` entry
    naming a missing directory silently, so nothing fails until
    ``CMD`` runs and the interpreter reports ``No module named
    telegram_invite_bot`` — a clean build, a green push, and a
    container that crash-loops on first start.

    Pinned here rather than by building the image, for the reason given
    at the top of this module: the structural claim is what regresses,
    and a daemon-free assertion catches it in the same second.
    """
    builder, runtime = dockerfile_text.split("AS runtime", 1)
    installs = [
        line.strip()
        for line in builder.splitlines()
        if line.strip().startswith("RUN ") and "pip install" in line
    ]
    assert installs, "builder stage must install the package"
    assert not any(" -e ." in line or " --editable" in line for line in installs), (
        "the package must be installed into the venv, not linked to /build/src: "
        "an editable install does not survive the discarded builder stage"
    )
    assert "/build/src" not in runtime, (
        "runtime stage must not depend on the builder's source directory"
    )
