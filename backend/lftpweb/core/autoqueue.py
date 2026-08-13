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
3. **Suppression (§4.6).** `auto_queue_suppressed`, plus the terminal states it's paired
   with (`STOPPED`, `FAILED`, `REMOVED_LOCAL`, `REMOVED_BOTH`), are excluded by construction:
   the eligibility query only ever selects `REMOTE_ONLY`/`PARTIAL` items with the flag clear.
   A `STOPPED` item whose pattern still matches must never be picked up here.
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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from lftpweb.core import mount_sentinel, patterns, settle

logger = logging.getLogger(__name__)

# DESIGN.md §4.6/§4.7: only a top-level item with no active job, not suppressed, and not yet
# complete is eligible. Everything else -- QUEUED, DOWNLOADING, DOWNLOADED, STOPPED, FAILED,
# EXCLUDED, REMOVED_LOCAL, REMOVED_BOTH, and any post-processing state -- is excluded by
# *not* being named here, rather than by naming every excluded state explicitly.
ELIGIBLE_STATES = ("REMOTE_ONLY", "PARTIAL")


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

        cursor = await self.db.execute(
            "SELECT id, rel_path, is_dir FROM item WHERE queue_id = ? "
            "AND instr(rel_path, '/') = 0 AND auto_queue_suppressed = 0 "
            f"AND state IN ({','.join('?' for _ in ELIGIBLE_STATES)})",
            (queue.id, *ELIGIBLE_STATES),
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
