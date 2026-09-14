# syntax=docker/dockerfile:1.7
# ------------------------------------------------------------------
# Multi-stage build for telegram-invite-bot.
#
# Stage layout:
#   * `builder`  — installs deps into a venv under /opt/venv. Has
#                  build-essentials, headers, the wheel cache. NEVER
#                  ends up in the final image.
#   * `runtime`  — copies the venv + source. No compilers, no apt
#                  cache. Runs as a non-root user. This is the
#                  artifact that gets pushed to a registry.
#
# Pinning rationale (read before bumping):
#   * Python 3.11 mirrors pyproject's `requires-python` and the
#     CI matrix. Bumping beyond 3.12 needs a deliberate test pass —
#     aiosqlite and a couple of legacy modules touch StrEnum and
#     CPython internals that drifted in 3.13.
#   * `slim-bookworm` over `alpine`: musl breaks aiohttp/uvloop
#     wheels (we'd fall back to building from source on every CI
#     run). The size delta after the multi-stage trim is ~12 MB,
#     not worth the wheel-compatibility risk.
# ------------------------------------------------------------------

# ----- Stage 1: builder -------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Build deps for any wheel that doesn't ship a manylinux build —
# Pillow's image codecs and aiohttp's optional C accelerator are the
# usual offenders. Pinned to the slim base's `bookworm` apt suite.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build

# Copy ONLY dep manifests first so layer-cache survives source-only
# changes. A code-only commit reuses the heavy `pip install` layer.
COPY pyproject.toml uv.lock* ./
# `pyproject.toml` declares `readme = "README.md"`, so the build backend
# reads it during `pip install .` below — it has to be here, not with
# the rest of the source. It changes about as often as the manifests do,
# so the cache cost is nil.
COPY README.md ./

# Install runtime deps from pyproject (no dev extras). Using pip
# directly — uv is great for local dev but adds a build-time
# dependency on a fast-moving binary that doesn't ship in slim.
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir .

# Now the source. Separate COPY so a docs/lint change still hits the
# cached pip-install layer above.
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

# Install the package itself, now that there is one to install. The
# `pip install .` above ran against a tree with no `src/`, and
# hatchling's wheel target is `src/telegram_invite_bot`, so it built a
# wheel holding nothing but metadata. That is what was wanted there —
# the dependency set, in its own cacheable layer — but it leaves the
# package itself still to be installed. `--no-deps` because they are
# already in; pip replaces the metadata-only distribution in place.
#
# NOT `-e .`. Hatchling spells an editable install as a `.pth` file
# holding one absolute path, `/build/src` — and `/build` is a
# builder-stage directory that does not exist in the runtime image.
# `site.py` discards a `.pth` line pointing at a missing directory
# without a word, so the image would build clean, start, and die on
# `No module named telegram_invite_bot`. A real install puts the code
# inside the venv, which is the one thing the runtime stage copies.
RUN pip install --no-cache-dir --no-deps .


# ----- Stage 2: runtime -------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app

# Runtime-only system deps: CA certs for outbound HTTPS (Telegram,
# DeepSeek, OpenWeather), tini for proper PID-1 signal forwarding
# (uvicorn shutting down cleanly on SIGTERM matters for the
# blue/green cutover — half-served requests must drain).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Fixed UID/GID so a bind-mounted ./database survives
# `docker compose down` + `up` without chowning on the host. Match
# the host user that owns the .db files in dev (1000 is the typical
# first non-system UID on Debian/Ubuntu/macOS); override at build
# time with `--build-arg APP_UID=...` if your host differs.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --system --gid "${APP_GID}" app \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
        --create-home --home-dir "${APP_HOME}" --shell /usr/sbin/nologin app

WORKDIR ${APP_HOME}

# Bring the prepared venv across, owned by the unprivileged app user
# that runs it.
# The application code rides along inside the venv (installed, not
# linked), so there is no `src/` to copy — only the two things Alembic
# reads from the filesystem at runtime.
COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --from=builder --chown=app:app /build/migrations ./migrations
COPY --from=builder --chown=app:app /build/alembic.ini ./alembic.ini

# Database + logs live on bind mounts (declared in compose). The
# directories must exist owned by `app` so the process can write
# before the volume is attached on first boot.
RUN mkdir -p ${APP_HOME}/database ${APP_HOME}/logs \
    && chown -R app:app ${APP_HOME}

USER app

# Bind all interfaces *here* and only here. The application defaults to
# loopback (see ``config/settings.py``), which is right on a bare host
# behind nginx and wrong in a container: a process bound to 127.0.0.1
# inside the namespace cannot be reached through the published port, so
# the container would come up healthy-looking and serve nobody. Inside
# the namespace the container boundary is the firewall, and the only
# way in is the port docker was told to publish.
ENV HOST=0.0.0.0

EXPOSE 8080

# Healthcheck hits /healthz — liveness only, no DB touch. That is the
# point: a container is restarted when the process is wedged, not when
# SQLite is briefly locked, and the engine probe that would conflate
# the two lives on /readyz for an external monitor to poll. The
# 30s/10s/3 cadence trips within ~90s of a real wedge. Docker treats
# exit 0 = healthy, 1 = unhealthy. `--quiet --tries=1 --spider` so
# wget doesn't write the response body to disk.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:8080/healthz || exit 1

# `tini` reaps zombies and forwards SIGTERM to uvicorn so the
# graceful shutdown path actually runs. Without it, `docker stop`
# would SIGKILL the process after the grace period and any
# half-served webhook update would be lost.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "telegram_invite_bot", "--mode=webhook"]
