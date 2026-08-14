""" "Folder prefix during transfer" (2026-08-14,
`prompts/2026-08-14-in-flight-folder-prefix.md`) -- a directory item downloads into
`<local_path>/<prefix><name>/` instead of `<local_path>/<name>/`, and is renamed to its real
name only once the transfer is fully complete, so an importer (Sonarr, Radarr, ...) polling the
download tree can never see a partial multi-file release. Live incident, 2026-08-13/14: Sonarr
imported the episodes of a `mirror` job that had finished, then its own post-import cleanup
deleted the release folder while lftp was still writing the last two -- lftp died mid-rename,
`ENOENT`, for both.

**Directory items only.** A single-file `pget` item is complete the instant lftp renames it off
`.lftp` (§4.4b) -- there is no window in which an importer can see a partial release, because
the release *is* that one file. Nothing in this module (or its callers) ever sets a prefix for a
`pget` job.

**This reverses part of a phase 5 decision, not a re-litigation of it** -- see
`docs/decisions.md` (2026-08-14 entry) for the full "what changed" argument. Phase 5 rejected
making a transfer's *destination* differ from `local_path` because it would mean the reconciler
comparing remote-vs-local at a different root during a transfer than after one completes. That
cost is real here too, and paid the same way phase 5 already scoped `staging_path`: the
*physical* write target during transfer is `<local_path>/<prefix><name>/`, but `item.rel_path`
-- the identity the reconciler matches against the remote tree, `item_settle` is keyed by, and
auto-queue patterns evaluate -- **never** carries the prefix. `core/queue.py._spawn_decision`
computes the physical path at spawn time from this module's resolution; nothing else in the
codebase (reconciler, settle gate, patterns, postprocessing) needs to know the prefix exists,
because by the time any of them looks at an item, `_reap_one` has already renamed it back to its
real name (see that method's own comment for exactly when).

**Site-wide + per-queue, inherit-or-override** (`3500b3f`'s shape, not the AND it replaced):
`DownloadPrefixSettings` is the site default (JSON in `setting`, same pattern as
`core/queue.py.TransferSettings`); `path_queue.download_prefix_enabled`/`download_prefix`
(migration 017) are nullable columns where `NULL` means "inherit the site value," resolved
independently of each other by `resolve_for_queue` below -- never ANDed.

**Default off.** A fresh install, and every queue on an existing install, gets this switched off
until someone turns it on -- this project's rule for every new capability
(`prompts/startnewsession.md`). Unlike the settle gate (which also ships a behaviour change),
this one was left off: it moves where in-flight bytes physically live, which an install with a
transfer already running when it upgrades would notice immediately as an on-disk path change.
That is the caller's decision to make, not this task's -- flagged in the task's own report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.mount_sentinel import SENTINEL_NAME

if TYPE_CHECKING:
    import aiosqlite

DEFAULT_PREFIX = ".downloading-"

# A prefix must not collide with lftpweb's own other filesystem conventions -- `core/extract.py`'s
# staging directories and `core/mount_sentinel.py`'s sentinel file. "Collide" is checked both
# ways (candidate a prefix of a reserved name, or vice versa): every filter that recognises one
# of these names (`core/local_scan.py`) does so with a `str.startswith` check, so either
# direction of overlap would make one convention swallow the other's entries.
RESERVED_NAMES = (UNPACK_PREFIX, FAILED_PREFIX, SENTINEL_NAME)


def validate_prefix(prefix: str, *, enabled: bool) -> str | None:
    """Server-side validation (this task's own requirement) -- returns an error message, or
    `None` when `prefix` is acceptable.

    **Non-empty is only required while `enabled` is true.** A disabled setting's prefix value
    is inert, so a blank field (the common case on a fresh install, or a per-queue override that
    only ever touches `enabled` and leaves the prefix on inherit) must not block saving the rest
    of the form. **Every other check runs unconditionally, regardless of `enabled`** -- shape
    (no path separator, no `.`/`..`) and the reserved-name collision check both guard against a
    genuinely bad value ever reaching the database at all, since a value saved while disabled
    can still take effect later without going through this function again (e.g. a per-queue
    `download_prefix` override saved while that queue's own `download_prefix_enabled` is left on
    inherit, with the *site-wide* toggle switched on afterwards).
    """
    if not prefix:
        return "prefix must not be empty while the setting is enabled" if enabled else None
    if "/" in prefix or "\\" in prefix:
        return "prefix must not contain a path separator"
    if prefix in (".", ".."):
        return "prefix must not be '.' or '..'"
    for reserved in RESERVED_NAMES:
        if prefix == reserved or prefix.startswith(reserved) or reserved.startswith(prefix):
            return (
                f"prefix {prefix!r} collides with lftpweb's own {reserved!r} convention -- "
                "choose a prefix that shares no leading substring with it"
            )
    return None


# --- Site-wide settings (JSON in `setting`, the same pattern core/queue.py.TransferSettings /
# core/postprocess.py.PostprocessSettings / core/settle.py.SettleSettings all use) ------------

SETTING_KEY = "download_prefix_settings"


@dataclass(frozen=True)
class DownloadPrefixSettings:
    enabled: bool = False
    prefix: str = DEFAULT_PREFIX


async def load_download_prefix_settings(db: "aiosqlite.Connection") -> DownloadPrefixSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return DownloadPrefixSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return DownloadPrefixSettings()
    return DownloadPrefixSettings(
        enabled=bool(data.get("enabled", False)),
        prefix=str(data.get("prefix", DEFAULT_PREFIX)),
    )


async def save_download_prefix_settings(
    db: "aiosqlite.Connection", settings: DownloadPrefixSettings
) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps({"enabled": settings.enabled, "prefix": settings.prefix})),
    )
    await db.commit()


def resolve_for_queue(
    queue_enabled: bool | None,
    queue_prefix: str | None,
    site: DownloadPrefixSettings,
) -> tuple[bool, str]:
    """Inherit-or-override, resolved independently for each of the two fields -- following
    `3500b3f` (`core/postprocess.py._effective`'s shape), not the AND-of-two-toggles it
    replaced. `queue_enabled`/`queue_prefix` come straight off a `path_queue` row (migration
    017): `None` means "inherit," an explicit value means "this queue's own override,"
    independent of whether the *other* field is also overridden. So a queue can use the
    site-wide toggle but its own prefix string, or vice versa.
    """
    enabled = queue_enabled if queue_enabled is not None else site.enabled
    prefix = queue_prefix if queue_prefix is not None else site.prefix
    return enabled, prefix


def prefixed_name(prefix: str, name: str) -> str:
    """The on-disk directory name for `name` while its transfer is in flight."""
    return f"{prefix}{name}"
