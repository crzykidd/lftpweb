#!/bin/sh
# PUID/PGID/UMASK entrypoint (DESIGN.md §11.2). This is the part most likely to be got
# wrong, so every deviation from the obvious implementation is commented.
set -eu

# If the container was already started as non-root (compose's native `user: "UID:GID"`,
# §11.2's other supported path), there is nothing for this entrypoint to fix ownership of
# and no privilege to drop — just run the command.
if [ "$(id -u)" -ne 0 ]; then
    echo "lftpweb: running as non-root (uid=$(id -u)); skipping PUID/PGID setup" >&2
    exec "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"
umask "$UMASK"

# Deliberately *not* creating a passwd/group entry for PUID/PGID (no addgroup/adduser):
# that needs to write /etc/passwd and /etc/group, which live on the root filesystem —
# read-only in production (§11.1). su-exec and chown both accept raw numeric uid:gid
# without an NSS entry, so nothing here actually needs one; we just lose a friendly name
# in this log line, which is a fine trade for not needing a writable /etc.
echo "lftpweb: running as ${PUID}:${PGID}, umask ${UMASK}"

# Verify a path is writable *as the identity we're about to drop to*, by probing with
# su-exec rather than trusting `stat` — permission bits alone don't account for NFS
# squashing, ACLs, or a read-only mount, and the probe is what will actually be true for
# every request the app makes afterwards.
check_writable() {
    path="$1"
    fatal="$2"
    probe="${path}/.lftpweb-write-test.$$"
    if su-exec "${PUID}:${PGID}" sh -c "touch '$probe' 2>/dev/null && rm -f '$probe'"; then
        return 0
    fi
    if [ "$fatal" = "1" ]; then
        echo "lftpweb: ERROR: '${path}' is not writable by uid=${PUID} gid=${PGID}." >&2
        exit 1
    fi
    # Severity must match behaviour. This branch is advisory, so it says WARNING — an
    # "ERROR:" here reads as a failed startup for something the app carries on past.
    echo "lftpweb: WARNING: '${path}' is not writable by uid=${PUID} gid=${PGID}." >&2
    echo "lftpweb:          Only matters if a path queue is configured to use it." >&2
    return 0  # non-fatal: don't let `set -e` treat this function's own report as a script error
}

# True only for a path that is genuinely a separate mount — i.e. something the operator
# actually mounted in. An empty directory baked into the image (or the anonymous volume
# Docker materialises for a VOLUME declaration) shares its parent's device and is not one.
is_mountpoint() {
    _p="$1"
    [ -d "$_p" ] || return 1
    _d=$(stat -c %d "$_p" 2>/dev/null) || return 1
    _pd=$(stat -c %d "$_p/.." 2>/dev/null) || return 1
    [ "$_d" != "$_pd" ]
}

# /config only. Chowning is recursive because it's our own small app directory (db, logs,
# backups, keys) — never the data volumes below, which may be enormous NFS-backed media
# trees whose ownership belongs to the server exporting them, not to us. A chown failure
# here is unusual enough (this path is not expected to be an NFS export) that we still
# don't treat it as fatal by itself; the writability check right after it is what's fatal,
# because the app cannot run at all without a writable /config.
if [ -d /config ]; then
    # Captured via command substitution, not a redirect to a file: the production compose
    # profile runs a read-only root filesystem with only /config, /downloads, /staging and
    # a /run tmpfs writable (§11.1) — /tmp is not among them.
    if ! chown_err="$(chown -R "${PUID}:${PGID}" /config 2>&1)"; then
        echo "lftpweb: WARNING: chown /config failed: ${chown_err}" >&2
    fi
    check_writable /config 1
fi

# The per-job rc directory (DESIGN.md §4.2): every transfer writes a mode-0600 file here
# holding credentials and the known_hosts pin, then unlinks it on exit. It lives on the /run
# tmpfs so secrets never touch a persistent volume.
#
# It must be created and chowned *here*, as root, before privileges are dropped. Docker
# mounts the `tmpfs: - /run` from the compose file root-owned, so the app — correctly running
# as PUID — cannot create a subdirectory in it. Missing this meant every transfer failed at
# spawn with `PermissionError: '/run/lftpweb'` while scanning, browsing, and the whole UI
# looked perfectly healthy.
#
# Fatal, not advisory: without this directory no transfer can ever start, so failing at
# startup with one clear message beats failing per-job forever.
# Give PUID/PGID a real NSS identity before anything runs as them. /etc/passwd and
# /etc/group are symlinks into /run (see docker/Dockerfile) precisely so this works under a
# read-only root filesystem. Without it OpenSSH — and therefore every lftp transfer — fails
# with "No user exists for uid ${PUID}" while scanning and the whole UI look healthy.
for _f in passwd group; do
    if [ -f "/etc/${_f}.base" ]; then
        cp "/etc/${_f}.base" "/run/${_f}"
    elif [ ! -f "/run/${_f}" ]; then
        : > "/run/${_f}"
    fi
done
if ! grep -q "^[^:]*:[^:]*:${PUID}:" /run/passwd 2>/dev/null; then
    echo "lftpweb:x:${PUID}:${PGID}::/config:/sbin/nologin" >> /run/passwd
fi
if ! grep -q "^[^:]*:[^:]*:${PGID}:" /run/group 2>/dev/null; then
    echo "lftpweb:x:${PGID}:" >> /run/group
fi

RUN_DIR="${LFTPWEB_RUN_DIR:-/run/lftpweb}"
if ! mkdir_err="$(mkdir -p "$RUN_DIR" 2>&1)"; then
    echo "lftpweb: ERROR: could not create run dir '${RUN_DIR}': ${mkdir_err}" >&2
    echo "lftpweb:        Transfers cannot start without it. Is /run mounted read-only?" >&2
    exit 1
fi
chown "${PUID}:${PGID}" "$RUN_DIR" 2>/dev/null || true
chmod 0700 "$RUN_DIR" 2>/dev/null || true
check_writable "$RUN_DIR" 1

# Data volumes: never chowned, ever (§11.2) — see above. Under the usual root_squash NFS
# export, a root-owned entrypoint is squashed to `nobody` on the mount and a chown attempt
# would simply fail there, which is how these containers end up crash-looping against a
# perfectly healthy share. So we don't attempt it at all; we only verify writability under
# the target identity, and a failure here is a warning naming the path and the effective
# uid/gid — not fatal, because queues may simply not be configured yet.
# Only paths the operator actually mounted. `/downloads` and `/staging` are conventional
# defaults, not requirements — a queue's local_path can be any mounted path (e.g. an NFS
# share at /mnt/...). Checking them merely because the directory exists produced a confusing
# warning about `/downloads` for a deployment that never used it: the Dockerfile's
# `VOLUME ["/config", "/downloads"]` makes Docker materialise a root-owned anonymous volume
# there whenever nothing is mounted, which then fails the probe for no reason at all.
for data_path in /downloads /staging; do
    if is_mountpoint "$data_path"; then
        check_writable "$data_path" 0
    fi
done

exec su-exec "${PUID}:${PGID}" "$@"
