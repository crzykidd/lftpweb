# syntax=docker/dockerfile:1
#
# Multi-stage build (DESIGN.md §11): node builds the SPA, a python stage resolves and
# installs dependencies with uv, and the runtime stage carries forward only the venv, the
# built SPA, and the system tools lftp actually needs. No compiler in the final image.
#
# `frontend-dev` / `dev` are extra targets docker-compose.dev.yml builds from, for
# bind-mount + hot-reload development (uvicorn --reload / Vite dev server, §11.2). They
# are never used to produce the image that ships.
#
# Base images are pinned by digest, not just tag, so a silent retag can't change the
# archive tooling underneath a running deployment (§11.1). Bump deliberately.

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS frontend-dev
# Must match docker-compose.dev.yml's `user:` (the developer's own uid/gid, §11.2). The
# anonymous volume that keeps this stage's node_modules from being shadowed by the bind
# mount (see docker-compose.dev.yml) is populated *from this image*, root-owned by
# default, at container start — a non-root `user:` then can't write Vite's cache under it
# (EACCES on `node_modules/.vite`). Chowning at build time is what makes them match.
ARG DEV_UID=1000
ARG DEV_GID=1000
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci && chown -R "${DEV_UID}:${DEV_GID}" node_modules
# frontend/ itself is bind-mounted over /app by docker-compose.dev.yml; node_modules is
# not (see the anonymous volume there), so the `npm ci` above survives the mount.
EXPOSE 5187
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# WORKDIR is /app in every python stage, including runtime, and stays /app the whole way
# through — not /build, and not /opt/venv after a copy. A venv's activation scripts and
# console-script shebangs bake in an *absolute* path to the venv's own python; move the
# directory afterwards (e.g. builder's /build/.venv copied to runtime's /opt/venv) and
# every one of those shebangs points at a path that no longer exists in the new stage.
# Keeping the path identical across stages is what lets `COPY --from=` carry a working
# venv forward at all.
FROM python:3.13-alpine@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d AS python-base
# build-base covers any dependency lacking a musllinux wheel; today's dependency set
# (fastapi, uvicorn[standard], aiosqlite, pydantic, pydantic-settings) doesn't need it, but
# it's cheap insurance and stays out of the runtime image regardless.
RUN apk add --no-cache build-base
COPY --from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend/ backend/

FROM python-base AS python-builder
# --no-editable: the venv gets a real copy of lftpweb, independent of the source tree, so
# only .venv needs to be copied into the runtime stage.
RUN uv sync --locked --no-dev --no-editable

FROM python-base AS dev
# Editable (the default) + dev dependency group: installs a reference back to
# /app/backend/lftpweb rather than a frozen copy, so docker-compose.dev.yml bind-mounting
# fresh source over /app/backend takes effect without a rebuild.
RUN uv sync --locked
ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8087
CMD ["uvicorn", "lftpweb.main:app", "--host", "0.0.0.0", "--port", "8087", "--reload", "--reload-dir", "/app/backend"]

FROM python:3.13-alpine@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d AS runtime
# lftp: the transfer engine. openssh-client: ssh/sftp. 7zip: 7zz, the sole archive tool
# (rar/rar5/zip/7z/tar/gz/bz2/xz — see docs/decisions.md). su-exec: privilege drop after
# the entrypoint fixes ownership. tini: PID 1, reaps lftp children. curl: HEALTHCHECK.
RUN apk add --no-cache lftp openssh-client 7zip su-exec tini curl

WORKDIR /app
COPY --from=python-builder /app/.venv /app/.venv
COPY --from=frontend-builder /app/dist /app/static
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    LFTPWEB_STATIC_DIR=/app/static \
    LFTPWEB_CONFIG_DIR=/config \
    LFTPWEB_PORT=8087

# Deliberately NO `VOLUME` declaration.
#
# `VOLUME ["/config", "/downloads"]` made Docker materialise a root-owned *anonymous* volume
# at any of those paths the operator didn't mount — so a deployment whose queues live on an
# NFS share at /mnt/... still got a phantom /downloads it never asked for, failing the
# entrypoint's writability probe and warning about a path in no way in use. Anonymous volumes
# also accumulate invisibly and survive `docker compose down`.
#
# The compose files declare the real mounts explicitly, which is the only place that belongs.
# A queue's local_path may be any mounted path; /downloads is a convention, not a requirement.
EXPOSE 8087

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${LFTPWEB_PORT}/api/health" || exit 1

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "lftpweb.main:app", "--host", "0.0.0.0", "--port", "8087"]
