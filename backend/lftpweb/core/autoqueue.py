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
7. **The withhold gate (stage 3 of #18, `docs/transfers-redesign-spec.md` §4.3,
   `prompts/2026-08-23-withhold-and-cadence.md`).** Off by default like everything else in this
   list (`WithholdSettings.enabled`); when on, an eligible item is skipped -- not suppressed,
   `item.state` untouched -- when a configured download client reports an *explicit*, terminal
   `FAILED` verdict for its own remote path (`settle.find_client_failure`), checked ahead of and
   independent of the settle gate above so it also catches the case the settle gate's own
   fingerprint cannot: a died-partway download or a failed unpack whose bytes have permanently
   stopped growing reads as "settled" to a fingerprint check, and would otherwise sail straight
   through it and into `_enqueue_item`. Self-lifting by construction -- every pass re-checks
   `find_client_completion` on the same candidates *first*, so a later genuine success for the
   same release always wins over a stale `FAILED` entry a client's own history may still hold.
   `self.withheld` mirrors `self.gated`'s own "one audit event per transition, presence is the
   live signal" idiom, one level deeper (per item rather than per queue).

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
from typing import TYPE_CHECKING

import aiosqlite

from lftpweb.core import audit, mount_sentinel, patterns, settle
from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.preflight import PreflightRow

if TYPE_CHECKING:
    from lftpweb.core.clientsync import ClientSyncScheduler

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


WITHHOLD_SETTING_KEY = "withhold_settings"


@dataclass(frozen=True)
class WithholdSettings:
    """Site-level toggle for the withhold gate (stage 3 of #18,
    `docs/transfers-redesign-spec.md` §4.3, `docs/download-client-framework-spec.md` §14,
    `prompts/2026-08-23-withhold-and-cadence.md`) -- own JSON blob, own key, same pattern as
    `AutoQueueSettings`/`settle.SettleSettings` above it in this file.

    **Default `False` -- ship it off, same reasoning stage 2b's `client_skip_enabled` used.**
    The gate's own matching is not what's in doubt (`settle.find_client_failure` reuses the
    exact, already-shipped `_client_content_path_matches` component-boundary rule
    `find_client_completion` already relies on for the *positive* verdict) -- what's unverified
    is the vocabulary a wrong default here would act on: SABnzbd's history `status="Failed"`
    mapping to `TransferPhase.FAILED` is `docs/download-client-framework-spec.md` §13.4 guess
    #2, doc-derived from vendor docs and never yet confirmed against a real instance -- the
    connector's own module docstring flags it explicitly as the settle-gate skip's highest-risk
    guess, and this gate keys off the identical mapping.

    **The two wrong-default outcomes are not symmetric, and that asymmetry is the actual
    argument for `False`, not merely "every new capability ships off" caution.** Withholding
    wrongly (guess #2 is subtly wrong, a non-failure status somehow maps to `FAILED`) means a
    genuinely good release **silently never arrives** -- the audit event exists, but nothing
    routes it to the user's attention the way a missing download itself would, so the failure
    mode is quiet by default and a user has to go looking for the reason its own gate exists to
    supply. Not withholding wrongly (a genuine partial failure isn't caught) reproduces exactly
    today's behavior -- the settle gate transfers the half-written directory, precisely as it
    already does for every install running this codebase before this stage shipped. One wrong
    default makes things *worse than today*, silently; the other leaves things *exactly as bad
    as today*, which is the status quo every existing install already tolerates. Given a choice
    between those two failure directions on an unverified vocabulary, only one of them is a
    regression -- so `False` is the safer default, not just the cautious-by-convention one.

    Still switchable on (a future `Settings -> Transfer` control, out of this stage's scope) for
    anyone who has confirmed `Failed` -> `FAILED` against their own live SABnzbd and wants the
    protection now rather than waiting for #18's stage 1a capture (spec §13.3) to confirm it for
    everyone.
    """

    enabled: bool = False


async def load_withhold_settings(db: aiosqlite.Connection) -> WithholdSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (WITHHOLD_SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return WithholdSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return WithholdSettings()
    return WithholdSettings(enabled=bool(data.get("enabled", False)))


async def save_withhold_settings(db: aiosqlite.Connection, settings: WithholdSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (WITHHOLD_SETTING_KEY, json.dumps({"enabled": settings.enabled})),
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
    # Stage 2b of #18 (prompts/2026-08-23-settle-gate-skip.md): the settle-gate skip needs this
    # queue's own root to turn an item's `rel_path` into the absolute remote path a download
    # client's own `content_path` can be compared against (`remote_path.rstrip("/") + "/" +
    # rel_path`, the identical join `core/postprocess.py`/`core/arrsync.py` already use for the
    # same purpose). `""` is the conservative default for every call site/test built before this
    # field existed -- `settle._client_content_path_matches` reads an empty `item_remote_path`
    # as "never matches anything" rather than degenerating into a bare-prefix bug, so an old
    # caller that never sets this simply never gets a client-verdict skip, which is exactly
    # today's behavior.
    remote_path: str = ""
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
        # Stage 2b of #18 (prompts/2026-08-23-settle-gate-skip.md) -- the download-client
        # poller's own completed-transfer cache, consulted only when `settle.SettleSettings.
        # client_skip_enabled` is on. **Plain-attribute wiring, not a constructor argument** --
        # the same "can't hand over an instance that doesn't exist yet at construction time"
        # reason `Engine.postprocess`/`Engine.delete_in_flight` are wired this way in
        # `main.py`'s lifespan: `ClientSyncScheduler` is constructed *after* `AutoQueue` there
        # (`TransferQueue`/`AutoQueue`/`Engine` all have to exist first). `None` -- every
        # existing test in this module, and any deployment before this task ships -- means "no
        # client-sync source available," treated identically to `client_skip_enabled` being off
        # (this task's own "every uncertain path falls back to today's behavior" rule), never as
        # an error.
        self.client_sync: "ClientSyncScheduler | None" = None
        # The withhold gate's own transition tracking (stage 3 of #18) -- queue_id -> {item_id:
        # human-readable reason}, mirroring `self.gated`'s own "presence is the signal, only log
        # on a transition" idiom, one level deeper (per-item rather than whole-queue). Public
        # (no leading underscore), the same "a future API/UI surface can read this cheaply"
        # reasoning `self.gated` already established -- no new endpoint is wired up by this
        # stage, but the data is there rather than trapped behind a private name a later task
        # would have to first go make public. Wholesale-replaced per queue on every `on_scan`
        # pass that reaches the eligibility loop, exactly like `self._settle_preflight` -- every
        # withhold is re-derived fresh from the client's own current candidates each pass, so
        # there is nothing to merge and no flap tolerance to reason about.
        self.withheld: dict[int, dict[int, str]] = {}

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
            self.withheld.pop(queue.id, None)
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
            self.withheld.pop(queue.id, None)
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
        withhold_settings = await load_withhold_settings(self.db)
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
        # The withhold gate's own this-pass result (stage 3 of #18) -- item_id -> reason,
        # compared against `self.withheld.get(queue.id, {})` (last pass's result) once the loop
        # below finishes, exactly the way `settle_gated` is compared against nothing (it has no
        # transition-logging need) but `self.gated` above *is* compared, for the identical
        # "log once per transition, not once per pass" reason.
        withheld_this_pass: dict[int, str] = {}
        previously_withheld = self.withheld.get(queue.id, {})
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
            # The withhold gate (stage 3 of #18, `docs/transfers-redesign-spec.md` §4.3) -- a
            # third gate of the same kind as the mount gate and the settle gate, checked before
            # the settle gate's own logic and **independent of `settle_settings.enabled`**: an
            # explicit terminal `FAILED` verdict for this item's own remote path means the
            # seedbox is holding known-bad bytes (a died-partway download, a failed unpack), and
            # the settle gate's own fingerprint reads exactly that as "stopped growing" --
            # settled. Without this gate, an item whose fingerprint already reads settled (or a
            # queue running with the settle gate off entirely) has nothing else standing between
            # it and `_enqueue_item` below.
            #
            # **Every one of the following makes this a no-op, falling through to the exact same
            # behavior as before this stage existed:** `WithholdSettings.enabled` off (default),
            # `self.client_sync` never wired, `queue.remote_path` empty (`_client_content_path_
            # matches`'s own empty-root guard), an unreachable client / blank queue-or-history
            # response (`failed_transfers()` returns nothing), a queue-side or `UNKNOWN` phase
            # (`find_client_failure` only ever matches a terminal `FAILED`), an outright failure
            # with no on-disk path to report (no `content_path`, no match -- spec §4.3's "a
            # client failing outright needs no code," true here by construction), or a near-miss
            # path that isn't a genuine component-boundary match.
            if withhold_settings.enabled and self.client_sync is not None and queue.remote_path:
                item_remote_path = queue.remote_path.rstrip("/") + "/" + row["rel_path"]
                # Completion is checked first, always -- this *is* the self-lift rule (this
                # task's own non-negotiable: "a permanent block from a transient verdict is the
                # failure mode to design against"). A re-grab/repair/manual retry that later
                # lands a genuine `COMPLETED` verdict for the same path wins over a `FAILED`
                # entry the client's own history may still be holding from the earlier, dead
                # attempt -- no separate "was this withheld last pass" bookkeeping decides this;
                # every pass re-derives the verdict fresh from the client's own current
                # candidates, so a later success is simply "no failure match found this pass."
                completion = settle.find_client_completion(
                    item_remote_path, self.client_sync.completed_transfers()
                )
                if completion is None:
                    failure = settle.find_client_failure(
                        item_remote_path, self.client_sync.failed_transfers()
                    )
                    if failure is not None:
                        instance_id, instance_name, transfer = failure
                        reason = (
                            f"download client {instance_name!r} (id={instance_id}) reports "
                            f"{row['rel_path']!r} FAILED at {transfer.content_path!r}"
                            + (f" -- {transfer.error_message}" if transfer.error_message else "")
                        )
                        if row["id"] not in previously_withheld:
                            # Once per *transition* into withholding, not once per scan pass --
                            # the identical debounce `self.gated`'s own "auto-queue disabled"
                            # event already uses, one level deeper (per item, not per queue).
                            await audit.record_event(
                                self.db,
                                level="warning",
                                item_id=row["id"],
                                kind="autoqueue_withheld",
                                message=(
                                    f"queue {queue.id} ('{queue.name}'): item "
                                    f"{row['rel_path']!r} withheld from auto-queue -- {reason}"
                                ),
                            )
                        withheld_this_pass[row["id"]] = reason
                        continue
            # The settle gate's eligibility half (prompts/open-issues.md #2): skip -- not
            # suppress -- an item whose remote subtree hasn't held still yet. Left eligible
            # for the *next* pass rather than marked `auto_queue_suppressed`, since nothing
            # about the item itself is wrong; it just isn't done arriving. A no-op when the
            # setting is off (`load_settle_settings` defaults to disabled).
            #
            # Reads this item's own settle progress once (rather than calling
            # `settle.is_settled_in_db` and separately re-querying) so the same read serves both
            # the eligibility check (`settle.is_settled_from_progress`) and, when it's not
            # settled, the Preflight row's own tooltip inputs below
            # (2026-08-21, "the settling chip should have a mouseover that shows time details").
            settle_progress = (
                await settle.settle_progress_in_db(self.db, queue.id, row["rel_path"])
                if settle_settings.enabled
                else None
            )
            if settle_settings.enabled and not settle.is_settled_from_progress(settle_progress):
                # Stage 2b of #18 (prompts/2026-08-23-settle-gate-skip.md): reached only once
                # the settle gate's own fingerprint-based check above has already decided this
                # item is NOT settled -- a positive, terminal client verdict is a *second*, more
                # direct way to reach the same "safe to transfer" conclusion (spec §4.3: "SAB
                # completing *is* that same fact, reported by the process doing the writing"),
                # never a way to skip the gate faster than "not settled" already earned.
                #
                # **Every one of the following makes this a no-op, falling through to the exact
                # same hold-and-Preflight-row behavior below (this task's own non-negotiable):**
                # `client_skip_enabled` off (default), `self.client_sync` never wired (a
                # deployment/test that predates this task), `queue.remote_path` empty (a
                # `QueueAutoConfig` built before this field existed --
                # `settle._client_content_path_matches`'s own empty-root guard), an unreachable
                # client / blank queue-or-history response (`completed_transfers()` returns
                # nothing), a queue-side or `UNKNOWN` phase (`find_client_completion` only ever
                # matches a terminal `COMPLETED`), or a near-miss path that isn't a genuine
                # component-boundary match.
                client_verdict = (
                    settle.find_client_completion(
                        queue.remote_path.rstrip("/") + "/" + row["rel_path"],
                        self.client_sync.completed_transfers(),
                    )
                    if settle_settings.client_skip_enabled and self.client_sync is not None
                    else None
                )
                if client_verdict is not None:
                    instance_id, instance_name, transfer = client_verdict
                    # The audit trail this task's own handoff prompt calls "not optional
                    # decoration" -- naming the client instance and the verdict that permitted
                    # the skip is the only way anyone will work out why, the day this feature
                    # ever transfers something half-written on a wrong guess.
                    await audit.record_event(
                        self.db,
                        level="info",
                        item_id=row["id"],
                        kind="settle_client_skip",
                        message=(
                            f"queue {queue.id} ('{queue.name}'): item {row['rel_path']!r} "
                            f"skipped the settle gate -- download client {instance_name!r} "
                            f"(id={instance_id}) reports it COMPLETED at "
                            f"{transfer.content_path!r}"
                        ),
                    )
                    await self._enqueue_item(row["id"])
                    queued += 1
                    continue
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
                    # The tooltip's own inputs (`core/preflight.py.PreflightRow.wait_scans`/
                    # `wait_since`'s own docstring) -- `settle_progress` is `None` only when this
                    # item has no `item_settle` row yet at all (never scanned with the settle-
                    # aware path), which `is_settled_from_progress` above already treats as "not
                    # settled" the same conservative way; both fields fall through to `None`
                    # together in that case rather than a fabricated pair.
                    wait_scans=settle_progress.matched_scans
                    if settle_progress is not None
                    else None,
                    wait_since=settle_progress.first_matched_at
                    if settle_progress is not None
                    else None,
                )
                continue
            await self._enqueue_item(row["id"])
            queued += 1
        # Wholesale replace, not a merge -- see `self._settle_preflight`'s own comment in
        # `__init__` for why this source doesn't need `PreflightHold`'s flap tolerance. Written
        # even when `settle_gated` is empty (settle off, or every eligible item settled) so a
        # queue that just cleared its own gate doesn't wait for anything to expire.
        self._settle_preflight[queue.id] = settle_gated
        # The withhold gate's own transition-out logging (stage 3 of #18): any item that was
        # withheld last pass but isn't this pass -- whether because the client now reports it
        # `COMPLETED` (the self-lift rule), the `FAILED` verdict simply aged out of the client's
        # own cache, or the item left eligibility entirely (queued manually, deleted, suppressed)
        # -- gets one "cleared" event, the same "one event when it engages, one when it lifts"
        # rule the mount gate's `autoqueue_gated`/`autoqueue_ungated` pair already establishes.
        for item_id, reason in previously_withheld.items():
            if item_id not in withheld_this_pass:
                await audit.record_event(
                    self.db,
                    level="info",
                    item_id=item_id,
                    kind="autoqueue_withhold_lifted",
                    message=(
                        f"queue {queue.id} ('{queue.name}'): withhold on item {item_id} "
                        f"cleared -- was: {reason}"
                    ),
                )
        self.withheld[queue.id] = withheld_this_pass
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
