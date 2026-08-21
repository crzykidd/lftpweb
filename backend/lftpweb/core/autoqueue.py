"""Pattern-matching intake onto the job queue (DESIGN.md §4.7, §4.6, §12).

`core/patterns.py` decides *what* matches; this module decides *when* to act on it — the
split DESIGN.md §12 calls for explicitly. Owned alongside `Engine`/`TransferQueue` on
`app.state` (DESIGN.md §2) and invoked once per queue at the end of every successful scan
(`Engine.scan_queue`).

Five things this module must get right, in order of consequence:

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
   `DOWNLOADED` early, because the gate's other half lives in `core/queue.py._reap_one`. **An
   item skipped for this reason alone is also projected into the Preflight box** (this module's
   own "Preflight" section below, prompts/2026-08-20-preflight-waiting-sources.md) -- it is
   exactly "would be queued this pass if only the gate weren't holding it," the box's own
   definition of "waiting."
5. **`_UNPACK_`/`_FAILED_` top-level items are never auto-queued** (2026-08-15,
   `prompts/done/2026-08-15-arr-eventtype-and-unpack-autoqueue.md`; "show it, don't grab it,"
   user decision same date, `docs/decisions.md`). The user's seedbox runs SABnzbd, which stages
   an in-progress unpack into a `_UNPACK_<name>` directory on the *remote* side before renaming
   it to the release's final name -- the same `_UNPACK_`/`_FAILED_` prefixes
   `core/extract.py`/`core/local_scan.py` already use for lftpweb's own local extraction
   staging, reused here (`UNPACK_PREFIX`/`FAILED_PREFIX`, imported from `core/extract.py`
   rather than duplicated) because they happen to name the identical convention on the wire.
   Unlike `local_scan.py`'s filter, this is **not** scan-side: these show up as ordinary
   `REMOTE_ONLY` items in the Files tree on purpose -- "someone's staging is not content" still
   holds, but the user wants to *see* a 34 GB in-flight unpack sitting there, just never have
   auto-queue grab it while SAB might still be rewriting it underneath. The exclusion is
   therefore applied at eligibility, in this module, not at scan visibility -- a top-level
   item's name starting with either prefix is skipped outright, before pattern matching and
   regardless of `state`. A *manual* Queue click is untouched (same "explicit
   action beats a heuristic" reasoning as the settle gate above) -- once SAB finishes and
   renames the directory, the plain name becomes eligible normally, no special-casing needed.
6. **An item the bound *arr has already been handed is never picked up again** (2026-08-19,
   `prompts/done/2026-08-19-autoqueue-requeues-imported-item.md`, production defect).
   `ARR_IMPORT_INELIGIBLE_STATUSES` -- `notified`/`imported`/`cleaned`, never `detected` -- is
   applied in the eligibility query, alongside the state and suppression clauses. See that
   constant's own comment for the incident and for why `detected` must stay eligible.

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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import aiosqlite

from lftpweb.core import audit, mount_sentinel, patterns, settle
from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.preflight import PreflightRow

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

# `item.arr_status` values that mean "the bound *arr owns this release now" -- auto-queue never
# picks such an item up again, whatever its `state` reads (2026-08-19,
# `prompts/done/2026-08-19-autoqueue-requeues-imported-item.md`).
#
# Production, v0.2.6, `move` queue bound to Sonarr: an item finished, post-processing renamed it
# to its final name and pushed the scan command, Sonarr began moving the media file out, and the
# next scan read the leftovers as `PARTIAL` -- eligible -- so auto-queue re-queued a release
# whose seedbox source `core/arrsync.py` was about to delete on the confirmed import. The job sat
# in the queue for 97 minutes (the second occurrence), blocked `_maybe_cleanup` the whole time
# ("an active job exists for this item"), and then failed `REMOTE_GONE` on 0 bytes.
#
# `core/mount_sentinel.py`'s `PARTIAL` grace branch is the general half of this fix and is not
# *arr-specific; this is the half with no time bound. It matters because an import is not
# guaranteed to be quick: the same incident's season-pack case took ~19 minutes to move 38
# episodes out, longer than `mount_sentinel.DEFAULT_GRACE_S`, so the grace branch alone would
# have released it back to `PARTIAL` and re-queued it anyway.
#
# **`detected` is deliberately absent**, and that is the load-bearing part: an item is matched
# against the *arr's queue record (`arr_status = 'detected'`) by `core/arrsync.py._match_items`
# long *before* lftpweb has downloaded it -- the *arr's queue is populated by its own download
# client on the seedbox, on its own schedule -- so making `detected` ineligible would stop
# auto-queue fetching *arr-tracked releases at all, which is the entire feature. Only the three
# statuses that can only be reached *after* this codebase's own pipeline completed
# (`arrnotify.notify_arr` writes `notified`; `arrsync` writes `imported`, then `cleaned`) are
# listed. `dropped`/`gone` are absent for the same reason as `detected`: both are reachable for
# an item lftpweb has not finished, or ever started, downloading.
#
# Not a suppression: `auto_queue_suppressed` is untouched, so a *manual* Queue click still works
# (same "an explicit user action beats a heuristic" line the settle gate and the `_UNPACK_`
# exclusion already draw), and nothing here needs clearing by hand later.
ARR_IMPORT_INELIGIBLE_STATUSES = ("notified", "imported", "cleaned")

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

    `name` (mid-run scope addition to `prompts/done/2026-08-16-path-browse-dialog.md`) exists
    solely so `on_scan`'s new mount-gate audit events can name the queue in their message --
    the `event` table has no `queue_id` column (only `item_id`/`job_id`, migration 001), and a
    gating episode has neither: it's a whole-queue fact, not one item's. `perform_remote_delete`
    already writes `f"queue {queue_id} ('{queue_name}')..."` into its own message for exactly
    the same reason; this follows that precedent rather than inventing a second convention.
    """

    id: int
    name: str
    local_path: str
    auto_queue_enabled: bool
    patterns_only: bool
    # Migration 024 ("a short display name per queue"), threaded through here (2026-08-21, "the
    # columns moved around" fix) so a settle-gated Preflight row can carry the same queue tag
    # every other row on the page shows (`core/preflight.py.PreflightRow.queue_short_name`).
    # `None` -- both "no short name set" and every existing call site/test built before this
    # field existed -- degrades to the queue's full `name`, the same fallback
    # `lib/queueDisplayName.ts.queueDisplayName` already applies everywhere else.
    short_name: str | None = None


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
        # Preflight (prompts/2026-08-20-preflight-waiting-sources.md) -- one queue's
        # settle-gated rows, keyed by item id, *replaced wholesale* on every `on_scan` pass that
        # reaches the eligibility loop for that queue. See this module's own "Preflight" section
        # for why this is a plain replace rather than `core/preflight.py.PreflightHold`'s
        # flap-tolerant merge: unlike `core/arrsync.py`'s *arr source, whose own report can blink
        # for a beat, this source's "is it still gated" question is answered fresh from this same
        # process's own persisted state every pass, so a full replace is both simpler and
        # strictly more correct -- no stale row can outlive the very next successful scan, which
        # is exactly the "no duplicate at handover" guarantee this box promises. Popped for a
        # queue entirely (not merely left stale) the instant that queue's own pass returns early
        # -- auto-queue off, or mount-gated -- mirroring `self.gated`'s own "silent pop" idiom
        # just above, since both mean "nothing about this queue is being evaluated right now."
        self._settle_preflight: dict[int, dict[int, PreflightRow]] = {}

    async def on_scan(self, queue: QueueAutoConfig) -> int:
        """Evaluate one queue's newly- *and* previously-seen eligible items. Called after
        every successful scan pass, whether or not auto-queue is enabled for this queue (so
        `self.gated` and logging stay accurate either way). Returns how many items were
        queued, for logging/tests.
        """
        if not queue.auto_queue_enabled:
            # Silent pop, deliberately (mid-run scope addition, docs/decisions.md): turning
            # auto-queue *off* is not a gate recovery, it's the user choosing not to run it at
            # all, so there is nothing an "ungated" event would be reporting.
            self.gated.pop(queue.id, None)
            self._settle_preflight.pop(queue.id, None)
            return 0

        if not mount_sentinel.check(queue.local_path):
            reason = (
                f"local root {queue.local_path!r} is missing, unreadable, or has not yet "
                "completed a scan with the mount sentinel present"
            )
            if self.gated.get(queue.id) != reason:
                # Once per *transition* into gating, not once per scan pass -- the `self.gated`
                # dict entry is the existing debounce this log line already relied on; the new
                # audit event reuses it rather than adding a second mechanism (mid-run scope
                # addition to prompts/done/2026-08-16-path-browse-dialog.md: a mistyped
                # `local_path` used to be visible only here, a log line nobody was watching).
                logger.warning("auto-queue disabled for queue %d: %s", queue.id, reason)
                await audit.record_event(
                    self.db,
                    level="warning",
                    kind="autoqueue_gated",
                    message=f"queue {queue.id} ('{queue.name}'): auto-queue disabled -- {reason}",
                )
            self.gated[queue.id] = reason
            # The whole queue is blocked -- surfaced as the Preflight box's own banner (this
            # queue by name, with `reason` verbatim) rather than as rows, so any settle-gated
            # rows already cached for it must go too: showing both a "this queue is blocked"
            # banner and a stale per-item row from before it was blocked would be the exact
            # "fifty rows burying the one fact that matters" shape the banner exists to avoid.
            self._settle_preflight.pop(queue.id, None)
            return 0
        if self.gated.pop(queue.id, None) is not None:
            # A real recovery -- the gate was actually blocking this queue a moment ago, not
            # just "auto-queue happens to be checked every pass." Gives the gating episode a
            # visible end in the audit trail, the same "record both the delete and the delete
            # withheld" shape §7.3/§7.4's remote-delete audit events already follow.
            await audit.record_event(
                self.db,
                level="info",
                kind="autoqueue_ungated",
                message=f"queue {queue.id} ('{queue.name}'): auto-queue resumed -- local root is present, readable, and holds the mount sentinel",
            )

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
            "SELECT id, rel_path, is_dir, remote_size FROM item WHERE queue_id = ? "
            "AND instr(rel_path, '/') = 0 AND auto_queue_suppressed = 0 "
            f"AND state IN ({','.join('?' for _ in eligible_states)}) "
            # 2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md): this module's own
            # docstring has claimed "no active job" since it was written, but nothing in the
            # query enforced it -- it relied entirely on `state` never being `QUEUED`/
            # `DOWNLOADING` while a job is active, which holds today but was never asserted
            # here. Made explicit so the claim is true by construction rather than by every
            # other module continuing to agree with it forever.
            "AND NOT EXISTS (SELECT 1 FROM job WHERE job.item_id = item.id "
            "AND job.state IN ('queued', 'running')) "
            # The *arr hand-off gate (2026-08-19) -- see `ARR_IMPORT_INELIGIBLE_STATUSES`.
            # `COALESCE` because `arr_status` is NULL for every item on an unbound queue (and
            # for an unmatched item on a bound one), and SQLite's `NOT IN` over a NULL left-hand
            # side is NULL, not true -- without it this clause would silently exclude every
            # untracked item in the system.
            f"AND COALESCE(item.arr_status, '') NOT IN "
            f"({','.join('?' for _ in ARR_IMPORT_INELIGIBLE_STATUSES)})",
            (queue.id, *eligible_states, *ARR_IMPORT_INELIGIBLE_STATUSES),
        )
        rows = await cursor.fetchall()

        queued = 0
        settle_gated: dict[int, PreflightRow] = {}
        for row in rows:
            # "Show it, don't grab it" (user decision, 2026-08-15, docs/decisions.md): a SAB
            # in-progress unpack staged under `_UNPACK_<name>` (or a `_FAILED_` leftover) on
            # the *remote* side stays visible as an ordinary REMOTE_ONLY item in the Files
            # tree -- this is eligibility, not scan filtering -- but is never a valid
            # auto-queue target, regardless of state or matching patterns. Checked before
            # pattern matching so a broad `*` select pattern can never override it.
            if row["rel_path"].startswith(UNPACK_PREFIX) or row["rel_path"].startswith(
                FAILED_PREFIX
            ):
                continue
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
                # Preflight (prompts/2026-08-20-preflight-waiting-sources.md, this module's own
                # "Preflight" section below) -- reached only once every *other* eligibility
                # check above has already passed (pattern match, no active job via the query's
                # own `NOT EXISTS`, unsuppressed, not *arr-owned, not an in-progress unpack), so
                # this item would be enqueued THIS PASS if only the settle gate weren't holding
                # it -- exactly "waiting," never "not wanted" (a suppressed item or a
                # pattern-unmatched one never reaches this line at all). `status_label` reuses
                # `FileNode.substate`'s existing "settling" wording (`core/itemview.py`) rather
                # than inventing new vocabulary for the same state; `size_remaining_bytes` is
                # `None` -- the release is already fully present remotely, there is nothing
                # "left" from this source's own point of view (`core/preflight.py.PreflightRow`'s
                # own docstring, the settle-gate example it names).
                settle_gated[row["id"]] = PreflightRow(
                    source="settle",
                    queue_id=queue.id,
                    queue_name=queue.name,
                    queue_short_name=queue.short_name,
                    title=row["rel_path"],
                    status_label="Settling",
                    source_label=queue.name,
                    source_kind=None,
                    size_bytes=row["remote_size"],
                    size_remaining_bytes=None,
                    # This gate is bound by *scan count*, not a wall-clock estimate --
                    # `core/preflight.py.PreflightRow.remaining_s`'s own docstring is explicit
                    # that fabricating one here would be exactly the "invented estimate" the
                    # handoff prompt rules out. This source's own already-existing remaining
                    # figure is `size_bytes` above (`preflightSizeLabel`'s "remote — 22 GB"), not
                    # a time.
                    remaining_s=None,
                    # No separate download client in this source's own model -- lftpweb itself is
                    # the thing that will fetch this release once the gate releases it.
                    download_client=None,
                )
                continue
            await self._enqueue_item(row["id"])
            queued += 1
        # Wholesale replace, not a merge -- see `self._settle_preflight`'s own comment in
        # `__init__` for why this source doesn't need `PreflightHold`'s flap tolerance. Written
        # even when `settle_gated` is empty (settle off, or every eligible item settled) so a
        # queue that just cleared its own gate doesn't wait for anything to expire.
        self._settle_preflight[queue.id] = settle_gated
        if queued:
            logger.info("auto-queue: queued %d item(s) for queue %d", queued, queue.id)
        return queued

    # --- Preflight (docs/transfers-redesign-spec.md §4; this task,
    # prompts/2026-08-20-preflight-waiting-sources.md) -- the settle gate's own eligibility
    # check above is also this box's second source: an item that would be auto-queued this very
    # pass if only its remote fingerprint had held still. `core/preflight.py.PreflightRow` is
    # the shared, source-agnostic row shape; everything about *how* an item earns one --
    # pattern matching, suppression, the settle check itself -- stays in `on_scan` above,
    # exactly where `core/arrsync.py`'s own "Preflight" section keeps its *arr-specific
    # counterpart. ---------------------------------------------------------------------------

    def preflight_rows(self, active_queue_ids: Iterable[int]) -> list[PreflightRow]:
        """The Preflight box's own read (`api/jobs.py.get_preflight`) -- every currently-cached
        settle-gated row for a queue id in `active_queue_ids`. That set is the caller's own live
        "is this queue actually eligible for auto-queue right now" check (`auto_queue_enabled`
        and not currently mount-gated) -- mirroring `ArrSyncScheduler.preflight_rows`' own
        "filtered live by the caller, not by this cache's own staleness" contract, and
        `self.gated`'s own existing "read reflects right now" idiom -- so a queue that just
        turned auto-queue off or went mount-gated stops contributing immediately, the same
        instant `self.gated`/`self._settle_preflight` themselves would already reflect it, not
        after any hold window.

        Sorted by title, case-insensitively -- the same boring-default rule
        `ArrSyncScheduler.preflight_rows` already applies within its own source.
        """
        allowed = set(active_queue_ids)
        rows = [
            row
            for queue_id, gated in self._settle_preflight.items()
            if queue_id in allowed
            for row in gated.values()
        ]
        rows.sort(key=lambda r: r.title.casefold())
        return rows
