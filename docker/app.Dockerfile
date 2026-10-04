# syntax=docker/dockerfile:1
#
# Climate AI application image: the FastAPI app (serves the built web app at /), the worker
# and the host-networked HomeKit service all run from this one image with different commands
# (see docker-compose.yml). Build context is the repository root:
#
#   docker compose build app            # or: docker build -f docker/app.Dockerfile .
#
# Stages: web (node builds web/dist) -> pybuild (venv with api[homekit]) -> runtime (slim,
# non-root, no compilers, no node).
#
# Builds natively on amd64 and arm64 (Apple Silicon Docker Desktop, Raspberry Pi 4/5): both base
# images are multi-arch, and every pinned Python dependency ships a manylinux wheel for both
# (checked against PyPI for cp312 on linux/aarch64). No apt-get anywhere: the Debian slim Python
# image already carries tzdata and CA certificates, and nothing needs a compiler.
#
# The base images are pinned to the Debian release (trixie) so a rebuild never silently moves
# to a new glibc; the Python 3.12 / Node 22 patch releases still arrive with each rebuild.

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG NODE_IMAGE=node:22-trixie-slim

# --------------------------------------------------------------------------------------
# 1. Web app (Vue 3 + Vite PWA) -> /web/dist
# --------------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS web
WORKDIR /web
ENV CI=1 NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
# Dependencies first so source edits don't bust the npm cache layer.
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# `npm run build` = vue-tsc --noEmit (type check) && vite build -> dist/
RUN npm run build && test -f dist/index.html

# --------------------------------------------------------------------------------------
# 2. Python dependencies + the climate package (with the HomeKit extra) in /opt/venv
# --------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS pybuild
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
# No compiler on purpose. Every dependency installs from a wheel on amd64 and arm64; the one
# source-only package (chacha20poly1305 0.0.3, an aiohomekit dependency) is pure Python.
# --prefer-binary keeps it that way: if an unpinned transitive dependency ever publishes a new
# release as source first, pip takes the newest version that has a wheel instead of trying to
# compile.
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN pip install --upgrade pip setuptools wheel
COPY api/pyproject.toml /src/api/pyproject.toml
COPY api/climate /src/api/climate
# homekit extra = aiohomekit==4.0.1 (>= 4.0 carries the ecobee Sleep/Away UUID fix).
RUN pip install --prefer-binary "/src/api[homekit]" \
 && python -c "import climate, aiohomekit, importlib.metadata as m; print('climate', climate.__version__, 'aiohomekit', m.version('aiohomekit'))"

# --------------------------------------------------------------------------------------
# 3. Runtime
# --------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="climate-ai-app" \
      org.opencontainers.image.description="Climate AI API, worker and HomeKit service" \
      org.opencontainers.image.licenses="Private"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH \
    TZ=UTC \
    CLIMATE_WEB_DIR=/app/web \
    CLIMATE_HOMEKIT_STATE_DIR=/var/lib/climate/homekit

# Non-root user. The HomeKit state dir is created here with the right owner and mode 0700 so
# a fresh named volume mounted on it inherits both (it holds only the charmap.json cache; the
# pairing keys live encrypted in the database).
RUN groupadd --system --gid 10001 climate \
 && useradd --system --uid 10001 --gid climate --create-home --home-dir /home/climate \
        --shell /usr/sbin/nologin climate \
 && mkdir -p /var/lib/climate/homekit /app \
 && chown -R climate:climate /var/lib/climate \
 && chmod 700 /var/lib/climate/homekit

COPY --from=pybuild /opt/venv /opt/venv
# Container-local time for logs (TZ from .env); the app itself stores UTC and reasons about
# schedules in the house time zone saved in Setup. The Debian base ships /usr/share/zoneinfo;
# should a different PYTHON_IMAGE lack it, point the C library at the tzdata Python package.
RUN [ -d /usr/share/zoneinfo ] || ln -s "$(python -c 'import os, tzdata; print(os.path.join(os.path.dirname(tzdata.__file__), "zoneinfo"))')" /usr/share/zoneinfo
COPY --from=web /web/dist /app/web
# Admin scripts (pair_ecobee.py, export_schema.py): `docker compose run --rm homekit python
# scripts/pair_ecobee.py ...` runs from WORKDIR /app/api. The climate package itself is
# imported from the venv, not from a source checkout.
COPY api/scripts /app/api/scripts

WORKDIR /app/api
USER climate
EXPOSE 8000

# Only meaningful for the `app` command; docker-compose.yml disables it for worker/homekit.
# Healthy = HTTP 200 from /api/health AND the database answered (ok: true).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import json,sys,urllib.request as u; sys.exit(0 if json.load(u.urlopen('http://127.0.0.1:8000/api/health', timeout=4)).get('ok') else 1)"]

# X-Forwarded-* is trusted only from Docker bridge networks by default (see docker-compose.yml).
CMD ["uvicorn", "climate.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=172.16.0.0/12"]
