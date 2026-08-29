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
   definition of "waiting." A terminal client verdict (item 8 below) can lift this gate early,
   but only once a re-fingerprint has confirmed nothing moved across `settle.
   CLIENT_RECHECK_INTERVAL_S` (5s) -- see that item's own docstring. Gated behind `settle.
   SettleSettings.client_skip_enabled`, on by default (2026-08-29, docs/decisions.md).
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
8. **The client-shortened settle (2026-08-24, `prompts/2026-08-24-client-shortened-settle.md`;
   reworked 2026-08-29, `prompts/done/2026-08-29-settle-verify-under-existing-toggle.md`) -- gated
   by `settle.SettleSettings.client_skip_enabled`, on by default.** Once a client reports an item
   finished (`settle.find_client_completion`, matching `SEEDING` as well as `COMPLETED` -- see
   `settle.FINISHED_TRANSFER_PHASES`'s own comment), this instance's own background ticker
   (`start`/`_recheck_loop`/`advance_pending_rechecks` below) fingerprints the item's remote
   subtree (the ordinary settle gate's own `settle.compute_fingerprints`, reused rather than
   reinvented), waits `settle.CLIENT_RECHECK_INTERVAL_S` (5s), and fingerprints it again --
   queuing only if the two match, exactly the ordinary settle gate's own stability test on a
   faster clock. `on_scan` only *registers* a pending recheck here (never sleeps or fetches inside
   a scan pass); the ticker owns the actual wait and the eventual `_enqueue_item` call.

   **This used to be two mechanisms, and the toggle used to default off.** The 2026-08-24 version
   shipped this re-fingerprint verify default ON with no toggle of its own, alongside an older
   pure time-hold (`settle.CLIENT_COMPLETION_HOLD_S`/`client_completion_ready`) that trusted a
   terminal verdict once it was merely old enough, gated by this same
   `client_skip_enabled` flag but still off by default. The user rejected both properties in the
   same breath: *"There is a toggle already for Skip the wait on a download client's own verdict.
   This setting should be the one that still does the 5s verify."* The old time-hold path is
   deleted outright here, not kept as a degraded fallback -- **one toggle, one meaning: skipping
   the wait means verifying that nothing moved**, and there is no code path anywhere in this
   subsystem that queues on a download client's word alone (`docs/decisions.md` has the rejected
   alternatives).

   **The toggle itself flips to on by default the same day**, a second, independent reversal: the
   `False` default `client_skip_enabled` shipped with only ever protected against the old
   time-hold's failure mode (a wrong status-mapping guess transferring a half-written directory on
   the strength of a string). That failure mode is gone now that the flag gates a mechanism that
   verifies on the filesystem before queuing anything -- a wrong or missing client verdict costs
   nothing, since the ordinary settle gate above simply keeps running underneath it either way.
   The user's own call: *"yes, make it on by default since it verifies."*

**Retroactive by construction.** DESIGN.md §4.7: "adding a pattern re-evaluates the whole
known model, not just future scans." This module re-queries every eligible top-level item in
the queue on *every* pass rather than tracking "newly seen" items itself — an item stays
eligible (`REMOTE_ONLY`/`PARTIAL`, unsuppressed) until it's queued, so a pattern added today
is applied to everything already sitting there the very next scan, with no separate
"re-evaluate history" code path to keep in sync with the normal one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite

from lftpweb.core import audit, mount_sentinel, patterns, settle
from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.preflight import PreflightRow

if TYPE_CHECKING:
    from lftpweb.core.clientsync import ClientSyncScheduler
    from lftpweb.core.remote import HostConfig, RemoteConnectionPool

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

    **Default `False` -- ship it off, the same caution `settle.SettleSettings.
    client_skip_enabled` used to carry before it earned its own on-by-default reversal
    (2026-08-29, docs/decisions.md).** That flag now defaults on *because* it verifies a client's
    verdict on the filesystem before acting on it, rather than trusting the verdict outright --
    this gate has no equivalent verification step to fall back on (it acts on an explicit
    `FAILED` the moment it's seen, not a fingerprint comparison), so the reasoning that let
    `client_skip_enabled` flip does not transfer here. The gate's own matching is not what's in
    doubt (`settle.find_client_failure` reuses the
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


@dataclass(frozen=True)
class _PendingClientRecheck:
    """One item's own in-flight client-shortened-settle state (2026-08-24,
    `prompts/2026-08-24-client-shortened-settle.md`) -- **in-memory only**, a deliberate choice:
    a restart mid-recheck simply loses this bookkeeping, and the item falls back to the ordinary
    settle gate on the very next scan -- the safe direction, not a data-loss risk. The "nothing
    may publish a state it did not read back from a table" invariant (`core/itemview.py`'s own
    docstring) governs *published item state*; this is internal gate bookkeeping that is never
    itself published, so it does not apply here.

    `on_scan` only ever *creates* an entry (when a finished client verdict first appears for an
    item with no entry yet) or *drops* one (when the verdict, or the item's own eligibility,
    disappears) -- it never mutates `first_fingerprint`/`first_taken_at`. Only this instance's own
    background ticker (`AutoQueue.advance_pending_rechecks`) ever does that, and always by
    replacing the dict entry with a new, frozen instance rather than mutating fields in place --
    asyncio is single-threaded but interleaves at every `await`, so a frozen replace-not-mutate
    discipline means a reader mid-iteration never observes a half-updated record.
    """

    queue_id: int
    queue_name: str
    queue_remote_path: str
    rel_path: str
    instance_id: int
    instance_name: str
    # Both `None` until the ticker's first fetch actually lands -- registration never fetches
    # anything itself (`on_scan` must never sleep or do I/O inside a scan pass, this task's own
    # non-negotiable); only `advance_pending_rechecks` below does.
    first_fingerprint: "settle.Fingerprint | None" = None
    first_taken_at: float | None = None


# The pending-recheck ticker's own cadence (2026-08-24,
# prompts/2026-08-24-client-shortened-settle.md; recalibrated 2026-08-29,
# prompts/done/2026-08-29-settle-verify-under-existing-toggle.md, alongside `settle.
# CLIENT_RECHECK_INTERVAL_S`'s own drop from 10s to 5s) -- a decision, not an accident, the same
# way `settle.REQUIRED_SETTLE_SCANS`/`SETTLE_MIN_AGE_S`/`CLIENT_RECHECK_INTERVAL_S` are.
# Deliberately **independent of, and faster than, `core/engine.py`'s own scan cadence**
# (`DEFAULT_SCAN_INTERVAL_S`, 30s): an item sitting at the settle gate with no job of its own yet
# does not qualify for `queue_is_active`'s fast ~5s local-only pass (that predicate's own
# `substate == 'settling'` branch only ever fires *after* a job has already run once), so relying
# on `on_scan`'s own call cadence for the two fingerprints this mechanism needs would mean the
# second one might not land for up to a full `scan_interval_s` -- defeating "roughly 5 seconds"
# entirely.
#
# **Kept at half of `settle.CLIENT_RECHECK_INTERVAL_S`, not left equal to it.** The original
# 10s/5s pair used that same 2:1 ratio; simply leaving the tick pinned at its old absolute value
# while only the window shrank would have made the two equal (5s and 5s) -- the wrong choice, and
# specifically the one this task's own handoff prompt calls out by name. `advance_pending_rechecks`
# only ever checks whether the interval has elapsed *at its own tick boundaries*, so a tick equal
# to the window means the second fingerprint's due-check can land anywhere from just over one tick
# to just over two ticks after the first, depending on exactly where in the ticker's own cycle
# that first fingerprint happened to be taken -- for a 5s window with a 5s tick, that is "often
# close to 5s, but routinely rounding up to a full 10s," exactly doubling the wait a user who
# asked for "5 seconds" would actually observe on an unluckily-timed item. Halving the tick to
# 2.5s instead bounds the second fingerprint's due-check to at most one 2.5s tick past the 5s
# mark -- a real observed window of 5.0-7.5s, not 5-10s -- while still keeping the tick
# meaningfully coarser than the window it measures, so the ticker isn't doing needless extra
# fetches for items nowhere near due. Cheap when idle regardless of the exact number: a tick with
# nothing in `self._pending_recheck` does no I/O at all (`advance_pending_rechecks`'s own first
# check).
RECHECK_TICK_S = 2.5


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

        # The client-shortened settle's own pending-recheck state (2026-08-24,
        # prompts/2026-08-24-client-shortened-settle.md) -- queue_id -> {item_id:
        # _PendingClientRecheck}. `on_scan` only creates/drops entries; `advance_pending_rechecks`
        # (this instance's own background ticker) owns every mutation once an entry exists. See
        # `_PendingClientRecheck`'s own docstring for why in-memory is deliberate here.
        self._pending_recheck: dict[int, dict[int, _PendingClientRecheck]] = {}
        # Plain-attribute wiring, the identical "can't hand over an instance that doesn't exist
        # yet at construction time" reason `self.client_sync` above is wired this way --
        # `core/engine.py.Engine.pool` (a `core/remote.py.RemoteConnectionPool`) and the
        # site's `HostConfig` provider both come from `main.py`'s lifespan, after `AutoQueue`
        # itself is constructed. `None` -- every test, and any deployment before this task ships
        # -- means "no remote-scan capability available," which `_fetch_item_fingerprint` below
        # treats exactly like an unreachable client: no information, fall back to the ordinary
        # settle gate, never an error.
        self.remote_pool: "RemoteConnectionPool | None" = None
        self.host_provider: "Callable[[], Awaitable[HostConfig | None]] | None" = None
        self._recheck_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Starts this instance's own background recheck ticker (`_recheck_loop`,
        `RECHECK_TICK_S`) -- independent of, and not a replacement for, `core/engine.py.Engine`'s
        own scan loop, the same "several small independent schedulers, each with one narrow job"
        shape `ClientSyncScheduler`/`ArrSyncScheduler`/`BackupScheduler` already establish.
        Idempotent (a second call while already started is a no-op), matching every sibling
        scheduler's own `start()`.
        """
        if self._recheck_task is None:
            self._recheck_task = asyncio.create_task(
                self._recheck_loop(), name="lftpweb-autoqueue-recheck-loop"
            )

    async def stop(self) -> None:
        """Cancels and awaits the recheck ticker -- must run before `self.db`/`self.remote_pool`
        close, the same ordering constraint every other scheduler's `stop()` documents at its own
        call site in `main.py`."""
        if self._recheck_task is not None:
            self._recheck_task.cancel()
            try:
                await self._recheck_task
            except asyncio.CancelledError:
                pass
            self._recheck_task = None

    async def on_scan(self, queue: QueueAutoConfig, *, now: float | None = None) -> int:
        """Evaluate one queue's newly- *and* previously-seen eligible items. Called after
        every successful scan pass, whether or not auto-queue is enabled for this queue (so
        `self.gated` and logging stay accurate either way). Returns how many items were
        queued, for logging/tests.

        `now` (`time.time()`-comparable epoch seconds) -- injectable rather than read internally,
        the same `now`-override shape `core/settle.py.advance_settle`/`is_settled` and
        `core/clientsync.py.ClientSyncScheduler.run_once` already use. **Not currently consulted
        by anything in this method's own body** (2026-08-29,
        prompts/done/2026-08-29-settle-verify-under-existing-toggle.md, deleted alongside the
        completion-delay clock this parameter used to feed) -- kept in the signature anyway so
        existing call sites/tests that already pass it don't need touching, and so a future
        timing-sensitive check added here has somewhere to read a caller-injected clock from
        without re-threading a new parameter through every test. Defaults to `time.time()`; every
        production caller leaves it unset.
        """
        now = time.time() if now is None else now
        if not queue.auto_queue_enabled:
            # Silent pop, deliberately (mid-run scope addition, docs/decisions.md): turning
            # auto-queue *off* is not a gate recovery, it's the user choosing not to run it at
            # all, so there is nothing an "ungated" event would be reporting.
            self.gated.pop(queue.id, None)
            self._settle_preflight.pop(queue.id, None)
            self.withheld.pop(queue.id, None)
            self._pending_recheck.pop(queue.id, None)
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
            self._pending_recheck.pop(queue.id, None)
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
        # The client-shortened settle's own this-pass result (2026-08-24,
        # prompts/2026-08-24-client-shortened-settle.md) -- every item id that still has a
        # finished client verdict AND is still not settled this pass, so any pending entry for an
        # item that drops out of that set (settled by the ordinary gate, verdict gone, no longer
        # matching, suppressed) can be dropped from `self._pending_recheck[queue.id]` once the
        # loop finishes -- a "wholesale replace, carry forward what's still present" idiom applied
        # by set membership rather than by rebuilding the dict, since the ticker (not `on_scan`)
        # owns every *value* in this dict once an entry exists (`_PendingClientRecheck`'s own
        # docstring).
        pending_recheck_candidates_this_pass: set[int] = set()
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
                    item_remote_path, self.client_sync.finished_transfers()
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
                # The client-shortened settle (item 8 of this module's own docstring) -- gated on
                # `settle.SettleSettings.client_skip_enabled`, on by default (2026-08-29,
                # prompts/done/2026-08-29-settle-verify-under-existing-toggle.md; see that item's
                # own docstring for the full history, including the second, now-deleted mechanism
                # this toggle used to also gate). Only *registers* this item's pending recheck
                # when the toggle is on AND a finished verdict (`settle.FINISHED_TRANSFER_PHASES`
                # -- `COMPLETED` or `SEEDING`) exists and no entry is tracking it yet -- it never
                # fetches a fingerprint, sleeps, or enqueues here; `self.advance_pending_rechecks`
                # (this instance's own background ticker) owns all of that. The toggle off, a
                # `None` verdict, `self.client_sync` never wired, or `queue.remote_path` empty,
                # all mean the same thing: nothing to register, no remote I/O, exactly the
                # ordinary settle-gate behavior for this item.
                if (
                    settle_settings.client_skip_enabled
                    and self.client_sync is not None
                    and queue.remote_path
                ):
                    item_remote_path = queue.remote_path.rstrip("/") + "/" + row["rel_path"]
                    recheck_verdict = settle.find_client_completion(
                        item_remote_path, self.client_sync.finished_transfers()
                    )
                    if recheck_verdict is not None:
                        instance_id, instance_name, _transfer = recheck_verdict
                        pending_recheck_candidates_this_pass.add(row["id"])
                        queue_pending = self._pending_recheck.setdefault(queue.id, {})
                        if row["id"] not in queue_pending:
                            queue_pending[row["id"]] = _PendingClientRecheck(
                                queue_id=queue.id,
                                queue_name=queue.name,
                                queue_remote_path=queue.remote_path,
                                rel_path=row["rel_path"],
                                instance_id=instance_id,
                                instance_name=instance_name,
                            )
                        # Else: already tracked -- left alone. The ticker may be mid-recheck for
                        # it right now; `on_scan` never touches an existing entry's fingerprint/
                        # timing fields (`_PendingClientRecheck`'s own docstring).

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
        # Drop any pending recheck for an item that dropped out of this pass's own candidate set
        # -- settled by the ordinary gate, the client verdict disappeared, no longer matching, or
        # suppressed. **In place, not a wholesale dict replace** (unlike `self.withheld` above):
        # the ticker owns every *value* in this dict once an entry exists, so `on_scan` only ever
        # adds a brand-new entry or removes a stale one, never rebuilds the dict itself --
        # rebuilding wholesale here could drop an update the ticker made between this pass's own
        # top-of-loop read and this line (`_PendingClientRecheck`'s own docstring has the full
        # reasoning).
        queue_pending = self._pending_recheck.get(queue.id, {})
        for stale_item_id in list(queue_pending):
            if stale_item_id not in pending_recheck_candidates_this_pass:
                queue_pending.pop(stale_item_id, None)
        if queued:
            logger.info("auto-queue: queued %d item(s) for queue %d", queued, queue.id)
        return queued

    # --- The client-shortened settle's own ticker (2026-08-24,
    # prompts/2026-08-24-client-shortened-settle.md, item 8 of this module's own docstring) --
    # `on_scan` above only registers a pending recheck; everything below actually advances one:
    # fetching a fingerprint, waiting `settle.CLIENT_RECHECK_INTERVAL_S`, fetching again, and
    # queuing only on a match. Runs on its own `RECHECK_TICK_S` clock, independent of `on_scan`'s
    # own call cadence, so it never sleeps or blocks a scan pass. ------------------------------

    async def _recheck_loop(self) -> None:
        while True:
            try:
                await self.advance_pending_rechecks()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                logger.exception("client-shortened settle: recheck tick failed")
            await asyncio.sleep(RECHECK_TICK_S)

    async def advance_pending_rechecks(self, *, now: float | None = None) -> int:
        """One tick's worth of work over every queue's pending recheck. `now` is overridable
        (`time.time()`-comparable), the same shape `on_scan`'s own `now` parameter already uses,
        so a test can drive the 5s comparison window without sleeping real wall-clock seconds.
        Returns how many items this call actually queued, for tests/logging -- `on_scan`'s own
        `queued` return value's shape.

        For each pending item, in order:

        - **No fingerprint taken yet** -- fetch one now (`_fetch_item_fingerprint`). `None` back
          (no remote-scan capability wired, an unreachable host, a partial/failed scan, or the
          item no longer has any remote entries at all) means "no information" -- drop the entry
          and fall back to the ordinary settle gate, the identical "every uncertain path falls
          back to today's behavior" rule this whole subsystem already follows. Otherwise, record
          the fingerprint and `now` and wait for the next tick.
        - **A fingerprint was taken, but `settle.CLIENT_RECHECK_INTERVAL_S` hasn't elapsed since**
          -- not due yet; left untouched for a later tick.
        - **Due** -- fetch a second fingerprint and compare. **Different (or unfetchable this
          time) -> never shortcut: drop the entry and fall back to the ordinary settle gate**,
          exactly as this task's own brief requires. **Equal -> verified stable.** Re-verifies
          this item is still auto-queue-eligible right now (`_still_auto_queue_eligible`) before
          actually enqueuing -- the SQL eligibility check that first admitted this item into
          `self._pending_recheck` (in `on_scan`) can be up to several `RECHECK_TICK_S` ticks
          stale by the time a recheck actually converges, and the one thing that can genuinely
          change in that window without another `on_scan` pass observing it first is a user
          action (a manual suppress, stop, or delete) -- `_enqueue_item` itself is idempotent
          against a since-queued item but does **not** check suppression, so this re-check is
          the one thing standing between a stale registration and resurrecting a release the
          user explicitly told lftpweb to leave alone. Either way, the entry is dropped: settled
          or not, this pending recheck is finished.
        """
        now = time.time() if now is None else now
        queued = 0
        if not self._pending_recheck:
            return queued  # cheap no-op when idle -- no I/O at all
        for queue_id, items in list(self._pending_recheck.items()):
            for item_id, pending in list(items.items()):
                if pending.first_fingerprint is None:
                    fingerprint = await self._fetch_item_fingerprint(
                        pending.queue_remote_path, pending.rel_path
                    )
                    if fingerprint is None:
                        items.pop(item_id, None)
                        continue
                    items[item_id] = _PendingClientRecheck(
                        queue_id=pending.queue_id,
                        queue_name=pending.queue_name,
                        queue_remote_path=pending.queue_remote_path,
                        rel_path=pending.rel_path,
                        instance_id=pending.instance_id,
                        instance_name=pending.instance_name,
                        first_fingerprint=fingerprint,
                        first_taken_at=now,
                    )
                    continue
                if now - pending.first_taken_at < settle.CLIENT_RECHECK_INTERVAL_S:
                    continue  # not due yet
                second_fingerprint = await self._fetch_item_fingerprint(
                    pending.queue_remote_path, pending.rel_path
                )
                if second_fingerprint is None or second_fingerprint != pending.first_fingerprint:
                    # Different, or unfetchable this time -- never shortcut on a changed (or
                    # unconfirmed) fingerprint. Dropped, not retried in place; `on_scan` re-arms
                    # a fresh recheck next pass if the client still reports this item finished
                    # and it's still not settled -- a fresh two-fingerprint comparison starting
                    # from now, never treated as a continuation of the one that just failed.
                    items.pop(item_id, None)
                    continue
                items.pop(item_id, None)
                if not await self._still_auto_queue_eligible(item_id):
                    continue
                await audit.record_event(
                    self.db,
                    level="info",
                    item_id=item_id,
                    kind="settle_client_recheck_skip",
                    message=(
                        f"queue {queue_id} ('{pending.queue_name}'): item {pending.rel_path!r} "
                        f"skipped the settle gate -- download client {pending.instance_name!r} "
                        f"(id={pending.instance_id}) reports it finished, and its remote subtree "
                        f"fingerprint held unchanged across a {settle.CLIENT_RECHECK_INTERVAL_S:.0f}s "
                        "re-verification (verified, not just trusted)"
                    ),
                )
                await self._enqueue_item(item_id)
                queued += 1
        return queued

    async def _fetch_item_fingerprint(
        self, queue_remote_path: str, rel_path: str
    ) -> "settle.Fingerprint | None":
        """Fingerprints one top-level item's remote subtree by scanning its **queue's** own
        remote root (`queue_remote_path`) -- not a narrower per-item scan -- through the same
        `core/remote.py.RemoteConnectionPool.scan` `core/engine.py.Engine.scan_queue` already
        uses for the ordinary full scan, then reading `settle.compute_fingerprints`'s own output
        back out for just this `rel_path`. Scanning one level up (rather than `queue_remote_path
        + "/" + rel_path` directly) is deliberate, not incidental: a top-level item can itself be
        a bare file (DESIGN.md §4.7's granularity allows it), and `find <file> -mindepth 1`
        returns nothing for a file with no children -- scanning the parent instead means this
        reuses `compute_fingerprints` exactly as it already works for the ordinary settle gate,
        with no file-vs-directory special case of its own.

        Returns `None` -- "no information," never a fabricated verdict -- when: `self.
        remote_pool`/`self.host_provider` aren't wired (no test, and no deployment before this
        task shipped, has them); the host provider itself returns `None` (no host configured
        yet); the scan raises for any reason (network failure, auth failure, a busybox fallback
        that also fails); the scan comes back **partial** (`interpret_primary_scan_result`'s own
        warning) -- the identical "a partial scan proves nothing, hold rather than trust it" rule
        `core/settle.py.advance_settle`'s own docstring states for the ordinary gate, applied
        here to a single comparison rather than a persisted counter; or the item simply has no
        fingerprint in this scan's own output (vanished from the remote entirely). Every one of
        these reads as "abandon this recheck attempt," never as an error the ticker should raise
        (`_recheck_loop`'s own `except Exception` is the last-resort backstop, not the intended
        path for any of these).
        """
        if self.remote_pool is None or self.host_provider is None:
            return None
        try:
            host = await self.host_provider()
            if host is None:
                return None
            remote_tree, warning = await self.remote_pool.scan(host, queue_remote_path)
        except Exception:  # noqa: BLE001 - one bad fetch must never crash the ticker
            logger.warning(
                "client-shortened settle: re-fingerprint scan of %r failed",
                queue_remote_path,
                exc_info=True,
            )
            return None
        if warning:
            return None  # partial scan -- no reliable evidence either way, never trust it
        return settle.compute_fingerprints(remote_tree).get(rel_path)

    async def _still_auto_queue_eligible(self, item_id: int) -> bool:
        """Re-verified immediately before `advance_pending_rechecks` above actually enqueues a
        recheck-confirmed item -- see that method's own docstring for why. Mirrors `on_scan`'s
        own eligibility clauses (suppression, state, the *arr hand-off) minus the pattern match
        itself (pattern membership doesn't change from outside `on_scan`, and this item already
        matched a few ticks ago) and minus the active-job clause (`_enqueue_item` already
        re-checks that itself, idempotently).
        """
        autoqueue_settings = await load_autoqueue_settings(self.db)
        eligible_states = (
            ELIGIBLE_STATES_WITH_EXTERNALLY_REMOVED
            if autoqueue_settings.re_download_externally_removed
            else ELIGIBLE_STATES
        )
        cursor = await self.db.execute(
            "SELECT 1 FROM item WHERE id = ? AND auto_queue_suppressed = 0 "
            f"AND state IN ({','.join('?' for _ in eligible_states)}) "
            f"AND COALESCE(arr_status, '') NOT IN "
            f"({','.join('?' for _ in ARR_IMPORT_INELIGIBLE_STATUSES)})",
            (item_id, *eligible_states, *ARR_IMPORT_INELIGIBLE_STATUSES),
        )
        return await cursor.fetchone() is not None

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
