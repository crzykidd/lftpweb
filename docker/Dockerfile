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
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# The in-app Docs section imports the repo-root Markdown directly (`../../../../docs/*.md?raw`,
# 2026-08-14) so that `docs/` is the single source both GitHub and the running app read — see
# docs/decisions.md. That import reaches *outside* `frontend/`, so this stage's build context
# must contain the real repo layout, not just the frontend subtree: hence `WORKDIR /app/frontend`
# and this copy, rather than the flat `WORKDIR /app` + `COPY frontend/ ./` this used before.
# Caught only by the CI image build — `npm run build` on a dev machine passes either way,
# because the repo root is right there.
COPY docs/ /app/docs/
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

# unrar, built from RARLAB source (2026-08-12 fix — DESIGN.md §6, docs/decisions.md,
# core/extract.py's module docstring). Alpine's `7zip` package (7zz) ships with NO RAR codec
# at all — `7zz i` inside the runtime image lists no Rar/Rar5 handler, verified by building
# and inspecting this exact image, not by reasoning about upstream 7-Zip's feature list.
# Distros strip it because 7-Zip's RAR decoder derives from unRAR source, whose licence they
# won't ship in `main`. So there is no RAR-capable archive tool anywhere in Alpine's package
# indexes (`unrar`, `unar`, `p7zip`, `unrar-free` are all absent from both `main` and
# `community`) — building from source is the only path. UnRAR's own licence is freeware:
# redistributing the built binary is explicitly permitted (see NOTICE), decompression-only
# (never a RAR-compatible compressor, which this project never needed anyway).
#
# Reuses python-base's build-base rather than a fresh stage — it's already on this layer
# chain and this is a builder stage that ships nothing forward but the one stripped binary.
# `-static-libstdc++ -static-libgcc`: the runtime and dev stages carry no C++ runtime package
# (no build-base, no libstdc++ apk), so a dynamically-linked unrar would fail to start there
# with "Error loading shared library libstdc++.so.6" — confirmed by building it the naive way
# first and running the result in a bare python:3.13-alpine container. Statically linking
# those two leaves musl libc (present in every Alpine image) as the only runtime dependency,
# same as 7zz's own footprint.
FROM python-base AS unrar-builder
ARG UNRAR_VERSION=7.2.3
ARG UNRAR_SHA256=3995af0aa32b1505a566da053725551a1f0698dc42b2fdf7ba7d65db0d004e33
WORKDIR /build
RUN wget -qO unrarsrc.tar.gz "https://www.rarlab.com/rar/unrarsrc-${UNRAR_VERSION}.tar.gz" \
 && echo "${UNRAR_SHA256}  unrarsrc.tar.gz" | sha256sum -c - \
 && tar xzf unrarsrc.tar.gz \
 && make -C unrar -j"$(nproc)" LDFLAGS="-pthread -static-libstdc++ -static-libgcc" \
 && strip unrar/unrar

FROM python-base AS dev
# The same transfer/archive tooling the runtime stage installs. Without it the dev container
# can scan (asyncssh is pure Python) but every transfer dies `SPAWN_FAILED: FileNotFoundError`
# the moment core/lftp.py tries to exec `lftp` — a failure that only surfaces on the first
# Queue click, long after the environment looks healthy.
RUN apk add --no-cache lftp openssh-client 7zip
# unrar (see the unrar-builder stage above): the dev image has historically been missing
# tools the runtime stage had, which cost a whole debugging session (2026-08-12,
# prompts/startnewsession.md) — copied here explicitly rather than trusting that omission
# not to repeat itself for the one format this project exists to fetch.
COPY --from=unrar-builder /build/unrar/unrar /usr/local/bin/unrar

# Must match docker-compose.dev.yml's `user:` (the developer's own uid/gid, §11.2), same
# reasoning as the frontend-dev stage above — but here it is /etc/passwd, not file ownership,
# that matters. lftp shells out to OpenSSH, and OpenSSH fatals with "No user exists for uid N"
# when the running uid has no passwd entry: asyncssh has an env fallback (core/remote.py sets
# LOGNAME for exactly this), OpenSSH has none. So scanning would keep working while every
# transfer failed — the single nastiest failure mode this project has hit on real hardware.
# The runtime stage solves it differently, symlinking /etc/passwd into the /run tmpfs and
# writing it in the entrypoint, because there the uid is a runtime PUID/PGID and the root
# filesystem is read-only; this stage knows its uid at build time and can just create the user.
ARG DEV_UID=1000
ARG DEV_GID=1000
RUN if ! getent group "${DEV_GID}" >/dev/null; then addgroup -g "${DEV_GID}" devgroup; fi \
    && if ! getent passwd "${DEV_UID}" >/dev/null; then \
         adduser -D -H -u "${DEV_UID}" -G "$(getent group "${DEV_GID}" | cut -d: -f1)" devuser; \
       fi

# Editable (the default) + dev dependency group: installs a reference back to
# /app/backend/lftpweb rather than a frozen copy, so docker-compose.dev.yml bind-mounting
# fresh source over /app/backend takes effect without a rebuild.
RUN uv sync --locked
ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8087
CMD ["uvicorn", "lftpweb.main:app", "--host", "0.0.0.0", "--port", "8087", "--reload", "--reload-dir", "/app/backend"]

FROM python:3.13-alpine@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d AS runtime
# lftp: the transfer engine. openssh-client: ssh/sftp. 7zip: 7zz, zip/7z/tar/gz/bz2/xz — NOT
# rar (Alpine's build has no RAR codec; see the unrar-builder stage above and
# docs/decisions.md). su-exec: privilege drop after the entrypoint fixes ownership. tini:
# PID 1, reaps lftp children. curl: HEALTHCHECK.
RUN apk add --no-cache lftp openssh-client 7zip su-exec tini curl
# unrar: the rar/rar5 decoder 7zz doesn't have (see the unrar-builder stage above).
COPY --from=unrar-builder /build/unrar/unrar /usr/local/bin/unrar

WORKDIR /app
COPY --from=python-builder /app/.venv /app/.venv
COPY --from=frontend-builder /app/frontend/dist /app/static
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

# OpenSSH's client calls getpwuid() on its own uid and fatals with "No user exists for uid N"
# when there is no /etc/passwd entry — exactly the identity model §11.2 commits to (numeric
# PUID/PGID, no NSS entry, read-only root). lftp shells out to ssh, so every transfer died with
# `max-retries exceeded (No user exists for uid 2000)` while scanning worked fine (asyncssh has
# an env fallback; OpenSSH has none).
#
# /etc/passwd cannot be appended to at runtime under `read_only: true`, so it becomes a symlink
# into the /run tmpfs, which IS writable. docker/entrypoint.sh writes the real content there —
# base copies plus an entry for PUID/PGID — before dropping privileges. Keep this as the last
# step touching /etc: anything after it resolving a user would hit a dangling symlink.
RUN cp /etc/passwd /etc/passwd.base \
 && cp /etc/group  /etc/group.base \
 && ln -sf /run/passwd /etc/passwd \
 && ln -sf /run/group  /etc/group

EXPOSE 8087

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${LFTPWEB_PORT}/api/health" || exit 1

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "lftpweb.main:app", "--host", "0.0.0.0", "--port", "8087"]
