"""Pattern-matching intake onto the job queue (DESIGN.md §4.7, §4.6, §12).

`core/patterns.py` decides *what* matches; this module decides *when* to act on it — the
split DESIGN.md §12 calls for explicitly. Owned alongside `Engine`/`TransferQueue` on
`app.state` (DESIGN.md §2) and invoked once per queue at the end of every successful scan
(`Engine.scan_queue`).

Three things this module must get right, in order of consequence:

1. **Default off, per queue.** `QueueAutoConfig.auto_queue_enabled` mirrors
   `path_queue.auto_queue_enabled`, which defaults to `0` in the schema (migration 001) and
   is never flipped by a migration — enabling it is an explicit user action (DESIGN.md §4.7,
   docs/decisions.md).
2. **The mount gate (docs/decisions.md).** Before evaluating *anything* for a queue, its
   local root must pass `core/mount_sentinel.py.check()`. Failing that, this queue's pass
   does nothing at all — not deferred item-by-item — and the reason is logged and kept on
   `self.gated` for the API/UI to surface.
3. **Suppression (§4.6), and the two ways a local copy can go away.** `auto_queue_suppressed`,
   plus the terminal states it's paired with (`STOPPED`, `FAILED`, `REMOVED_BOTH`), are
   excluded by construction: the eligibility query only ever selects `ELIGIBLE_STATES` items
   with the flag clear. A `STOPPED` item whose pattern still matches must never be picked up
   here. `REMOVED_LOCAL` is deliberately **not** in `ELIGIBLE_STATES` by default (reverted,
   2026-08-12, docs/decisions.md) -- see the long comment on `ELIGIBLE_STATES` below for why a
   same-day attempt to include it unconditionally was wrong, and `AutoQueueSettings.
   re_download_externally_removed` for the opt-in that puts it back for anyone who wants it.
4. **The settle gate (prompts/open-issues.md #2, `core/settle.py`).** Off by default like
   everything else in this list; when `settle.SettleSettings.enabled` is on, a matched item is
   still skipped -- left for a later pass -- until its remote fingerprint has held for
   `settle.REQUIRED_SETTLE_SCANS` consecutive scans. This is the "cheap half" of the gate: it
   stops auto-queue from spawning a transfer for an item that is still visibly arriving. A
   *manual* Queue click bypasses this check entirely (`core/queue.py.enqueue_item` doesn't
   consult it) -- an explicit user action beats a heuristic -- but still can't reach
   `DOWNLOADED` early, because the gate's other half lives in `core/queue.py._reap_one`.

**Retroactive by construction.** DESIGN.md §4.7: "adding a pattern re-evaluates the whole
known model, not just future scans." This module re-queries every eligible top-level item in
the queue on *every* pass rather than tracking "newly seen" items itself — an item stays
eligible (`REMOTE_ONLY`/`PARTIAL`, unsuppressed) until it's queued, so a pattern added today
is applied to everything already sitting there the very next scan, with no separate
"re-evaluate history" code path to keep in sync with the normal one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from lftpweb.core import mount_sentinel, patterns, settle

logger = logging.getLogger(__name__)

# DESIGN.md §4.6/§4.7: only a top-level item with no active job, not suppressed, and not yet
# complete is eligible. Everything else -- QUEUED, DOWNLOADING, DOWNLOADED, STOPPED, FAILED,
# EXCLUDED, REMOVED_BOTH, and any post-processing state -- is excluded by *not* being named
# here, rather than by naming every excluded state explicitly.
#
# `REMOVED_LOCAL` is excluded here by default, on purpose -- reverted, 2026-08-12
# (docs/decisions.md), after a same-day attempt (prompts/open-issues.md issue 4) added it
# unconditionally and got the premise wrong. **There are exactly two ways an item's local copy
# goes away, and they need opposite treatment:**
#
#   1. lftpweb deleted it itself (`core/local_delete.py.delete_local` -- a manual delete from
#      Files, or the retention sweep). That write chooses `REMOVED_LOCAL` when a remote copy
#      still exists and `REMOVED_BOTH` when it doesn't (fixed 2026-08-13,
#      `prompts/2026-08-13-delete-must-mark-the-whole-subtree.md` -- it used to be an
#      unconditional `REMOVED_BOTH`; see docs/decisions.md), *and* sets `auto_queue_suppressed
#      = 1` in the same write, always. So a `delete_local` row genuinely can read bare
#      `REMOVED_LOCAL` and still be excluded here -- by `auto_queue_suppressed = 0` in the
#      query below, not by the state name. The state name is what tells the Files page the
#      truth about disk; the suppression flag is what stops the re-fetch. Conflating the two
#      is the exact bug this file's own history (case 2, `6d3bd95`) is about.
#   2. Something *outside* lftpweb moved it -- an `*arr` importer picking up a finished
#      release (DESIGN.md §7.2's ordinary, expected import), a human, a script. This is the
#      only path that ever produces a bare `REMOVED_LOCAL` with `auto_queue_suppressed` clear
#      (via §7.3's grace period), and it is what this setting governs.
#
# Making case 2 eligible again by default was the bug: on a `copy`-mode queue with auto-queue
# on, the remote copy is never deleted (`copy` mode never touches the remote), so the moment an
# importer moves the files out, the item is right back to matching its own select pattern --
# re-queued, re-downloaded, re-imported, forever, every scan interval. The concrete case that
# decided the default: Sonarr/Radarr importing locally on one schedule, and a separate cleanup
# script pruning the seedbox on another -- between the import and the remote cleanup running,
# the same release re-fetches on every scan and the importer is handed duplicates repeatedly.
# `move` queues are immune either way -- the remote copy is deleted on verified completion, so
# there is nothing left to re-fetch and the item simply reaches `REMOVED_BOTH` instead.
#
# So `REMOVED_LOCAL` stays out of the default eligible set, and `AutoQueueSettings.
# re_download_externally_removed` (default `False`) is what puts it back in, for anyone who
# genuinely wants a copy-mode queue to re-fetch what something outside lftpweb removed. Named
# and scoped by *who removed the file*, never by the state name alone -- "locally deleted" is
# ambiguous about the agent of deletion, which is the exact confusion that produced this bug.
# lftpweb's own deletions are excluded by `auto_queue_suppressed = 1`, under either setting
# value and regardless of which of the two states the row landed on -- that is not something
# this toggle can ever switch on, because the toggle only widens which *state names* are
# eligible and never clears suppression.
ELIGIBLE_STATES = ("REMOTE_ONLY", "PARTIAL")
ELIGIBLE_STATES_WITH_EXTERNALLY_REMOVED = ELIGIBLE_STATES + ("REMOVED_LOCAL",)

SETTING_KEY = "autoqueue_settings"


@dataclass(frozen=True)
class AutoQueueSettings:
    """Site-level toggle (`setting` table, JSON, no migration -- same shape as
    `core/settle.py.SettleSettings` / `core/local_delete.py.RetentionSettings`).

    **Default `False`.** This is not "every new capability ships off" caution alone -- the
    behaviour it enables is actively wrong for the common `copy`-mode-plus-importer shape this
    project is built around (see the long comment on `ELIGIBLE_STATES` above), so `False` is
    the *correct* default, not merely the cautious one. Flip it on only for a queue where a
    local copy going missing outside lftpweb should always be re-fetched.

    **Only matters for `copy`-mode queues.** In `move` mode the remote copy is already deleted
    by the time an item could ever read bare `REMOVED_LOCAL` -- it reaches `REMOVED_BOTH`
    instead -- so there is nothing left to re-fetch and this setting changes nothing for a
    `move` queue either way. `sync` mode is not yet built (DESIGN.md §7, "not scheduled").
    """

    re_download_externally_removed: bool = False


async def load_autoqueue_settings(db: aiosqlite.Connection) -> AutoQueueSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return AutoQueueSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return AutoQueueSettings()
    return AutoQueueSettings(
        re_download_externally_removed=bool(data.get("re_download_externally_removed", False))
    )


async def save_autoqueue_settings(db: aiosqlite.Connection, settings: AutoQueueSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            SETTING_KEY,
            json.dumps({"re_download_externally_removed": settings.re_download_externally_removed}),
        ),
    )
    await db.commit()


@dataclass(frozen=True)
class QueueAutoConfig:
    """The subset of `core/engine.py.QueueConfig` this module needs, kept separate so it
    doesn't have to import the engine module (mirrors `core/lftp.py.HostCreds`' reasoning).
    """

    id: int
    local_path: str
    auto_queue_enabled: bool
    patterns_only: bool


class AutoQueue:
    def __init__(
        self, db: aiosqlite.Connection, enqueue_item: Callable[[int], Awaitable[int]]
    ) -> None:
        self.db = db
        self._enqueue_item = enqueue_item
        # queue_id -> human-readable reason the mount gate is currently blocking this queue.
        # Absent entirely once the gate passes (or auto-queue is off) so its presence alone
        # is the "is this queue gated right now" signal the API exposes.
        self.gated: dict[int, str] = {}

    async def on_scan(self, queue: QueueAutoConfig) -> int:
        """Evaluate one queue's newly- *and* previously-seen eligible items. Called after
        every successful scan pass, whether or not auto-queue is enabled for this queue (so
        `self.gated` and logging stay accurate either way). Returns how many items were
        queued, for logging/tests.
        """
        if not queue.auto_queue_enabled:
            self.gated.pop(queue.id, None)
            return 0

        if not mount_sentinel.check(queue.local_path):
            reason = (
                f"local root {queue.local_path!r} is missing, unreadable, or has not yet "
                "completed a scan with the mount sentinel present"
            )
            if self.gated.get(queue.id) != reason:
                logger.warning("auto-queue disabled for queue %d: %s", queue.id, reason)
            self.gated[queue.id] = reason
            return 0
        self.gated.pop(queue.id, None)

        compiled = await patterns.compiled_for_queue(
            self.db, queue.id, patterns_only=queue.patterns_only
        )

        settle_settings = await settle.load_settle_settings(self.db)
        autoqueue_settings = await load_autoqueue_settings(self.db)
        eligible_states = (
            ELIGIBLE_STATES_WITH_EXTERNALLY_REMOVED
            if autoqueue_settings.re_download_externally_removed
            else ELIGIBLE_STATES
        )

        cursor = await self.db.execute(
            "SELECT id, rel_path, is_dir FROM item WHERE queue_id = ? "
            "AND instr(rel_path, '/') = 0 AND auto_queue_suppressed = 0 "
            f"AND state IN ({','.join('?' for _ in eligible_states)})",
            (queue.id, *eligible_states),
        )
        rows = await cursor.fetchall()

        queued = 0
        for row in rows:
            if not compiled.item_matches(row["rel_path"], is_file=not bool(row["is_dir"])):
                continue
            # The settle gate's eligibility half (prompts/open-issues.md #2): skip -- not
            # suppress -- an item whose remote subtree hasn't held still yet. Left eligible
            # for the *next* pass rather than marked `auto_queue_suppressed`, since nothing
            # about the item itself is wrong; it just isn't done arriving. A no-op when the
            # setting is off (`load_settle_settings` defaults to disabled).
            if settle_settings.enabled and not await settle.is_settled_in_db(
                self.db, queue.id, row["rel_path"]
            ):
                continue
            await self._enqueue_item(row["id"])
            queued += 1
        if queued:
            logger.info("auto-queue: queued %d item(s) for queue %d", queued, queue.id)
        return queued
