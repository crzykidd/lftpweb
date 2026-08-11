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
    echo "lftpweb: ERROR: '${path}' is not writable by uid=${PUID} gid=${PGID}." >&2
    if [ "$fatal" = "1" ]; then
        exit 1
    fi
    return 0  # non-fatal: don't let `set -e` treat this function's own report as a script error
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

# Data volumes: never chowned, ever (§11.2) — see above. Under the usual root_squash NFS
# export, a root-owned entrypoint is squashed to `nobody` on the mount and a chown attempt
# would simply fail there, which is how these containers end up crash-looping against a
# perfectly healthy share. So we don't attempt it at all; we only verify writability under
# the target identity, and a failure here is a warning naming the path and the effective
# uid/gid — not fatal, because queues may simply not be configured yet.
for data_path in /downloads /staging; do
    if [ -d "$data_path" ]; then
        check_writable "$data_path" 0
    fi
done

exec su-exec "${PUID}:${PGID}" "$@"
