"""The asyncio loop that owns scanning and holds the current model (DESIGN.md §2).

Phase 2 scope: no job queue, no scheduler, no lftp process — this is scanning and
reconciliation only. Every `scan_interval_s` (default 30s, DESIGN.md §5) — per queue, as of
migration 009 (prompts/done/2026-08-12-per-queue-scan-interval.md); `effective_scan_interval`
resolves each queue's own column against this site-wide default — and on demand via
`request_rescan()`, the engine re-loads `host` + `path_queue` from the database (so a config
change takes effect on the *next* cycle without a restart), scans each enabled queue's remote
and local trees, reconciles them (`core/reconcile.py`), persists the result to `item` rows, and
publishes the fresh model over `core/events.py` for `api/ws.py` to fan out.

**Simplification recorded, then partially restored.** DESIGN.md §5 originally specified two
cadences — remote scan every 30s, a faster local-only walk every 10s (the 1 Hz active-file poll
that covers the gap is phase 3's `ProgressSampler`). Phase 2 collapsed that into one combined
interval (docs/decisions.md's phase 2 entry) because nothing was producing local-only changes
on that timescale yet. `prompts/2026-08-14-adaptive-scan-cadence-when-active.md` restored the
local half: while a queue is active (`queue_is_active` below — a running job, an item mid
download/verify/extract, an item held at the settle gate, or post-processing in flight), an
additional local-only pass (`_scan_queue_local_only`, `ACTIVE_SCAN_INTERVAL_S`, default 5s)
runs *between* full scans, reconciling a fresh local walk against the **cached** remote tree
from this queue's last full scan (`_cached_remote_tree`) — never a fresh SSH round trip. The
remote/full cadence itself (`_next_due`/`_schedule_next`, per-queue `effective_scan_interval`)
is untouched by activity; see that docstring and docs/decisions.md's entry for this task for
why a local-only pass must never advance `item_settle` and must never run before a queue's
first successful full scan.

**The loop is a single serial `asyncio.Task` (`_loop`/`start`/`stop`), never concurrent
fan-out.** `scan_all` awaits each due queue's `scan_queue` one at a time, in the same task that
will next compute the following wake delay — this is the whole reason an overrunning scan can
never stack a second concurrent scan of the same queue (or of any other queue): there is
nowhere for a second call to run from until this one returns. A per-queue interval makes that
guarantee worth stating explicitly rather than leaving it implicit, because a 10s option is the
first place in this codebase an overrun (a slow shared seedbox's `find` taking longer than the
queue's own interval) becomes a realistic, not merely theoretical, occurrence — see
`_schedule_next`'s docstring for how the schedule itself stays correct when that happens.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import (
    download_prefix,
    local_delete,
    local_scan,
    mount_sentinel,
    patterns,
    postprocess,
    settle,
)
from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import ITEM_VIEW_COLUMNS_QUALIFIED, ItemView, item_view
from lftpweb.core.reconcile import ReconciledNode, reconcile
from lftpweb.core.remote import HostConfig, RemoteConnectionPool, RemoteEntry, RemoteScanError

logger = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_S = 30.0

# `prompts/2026-08-14-adaptive-scan-cadence-when-active.md`, restoring the local half of
# DESIGN.md §5's original two-cadence design (docs/decisions.md). The user's own rule,
# verbatim: "local refresh 5 seconds if there is an active job, arriving, downloading etc." A
# named module constant, not a bare literal, and not (yet) a settings row -- see
# `resolve_active_check_interval`'s docstring for how it composes with a queue's own configured
# interval, and this task's decisions.md entry for why a settings row was deferred.
ACTIVE_SCAN_INTERVAL_S = 5.0

# The item-lifecycle states `queue_is_active` treats as "a transfer or post-processing step is
# actively touching this item's files right now" -- DESIGN.md §3.2's transient vocabulary,
# minus QUEUED (waiting, not yet touching anything) and minus the settle-gate hold (REMOTE_ONLY
# + substate='settling'), which `queue_is_active` checks separately since it isn't a `state`
# value on its own.
_TRANSIENT_ACTIVE_STATES = ("DOWNLOADING", "VERIFYING", "EXTRACTING")


def resolve_active_check_interval(full_interval: float | None) -> float | None:
    """The cadence at which `Engine` re-evaluates whether a queue is active and, if so, runs
    another local-only pass (`_scan_queue_local_only`) -- layered on top of that queue's own
    resolved full-scan interval (`effective_scan_interval`), never replacing it.

    `None` propagates as `None`: a queue whose full-scan interval already resolved to
    "on-demand only" (§5's `scan_interval_s = 0` convention) must not gain a timer of any kind
    just by becoming active -- this task's own explicit requirement, proven by
    `tests/test_engine_scan_cadence.py`. Otherwise `min(full_interval, ACTIVE_SCAN_INTERVAL_S)`
    -- a queue already configured *faster* than the active floor keeps its own faster cadence
    rather than being slowed down to it (`min`, deliberately not "replace").
    """
    if full_interval is None:
        return None
    return min(full_interval, ACTIVE_SCAN_INTERVAL_S)


async def queue_is_active(
    db: aiosqlite.Connection,
    queue_id: int,
    postprocess_in_flight_ids: frozenset[int] = frozenset(),
) -> bool:
    """Whether queue `queue_id` has anything happening in it right now that the fast local-only
    pass exists to keep current -- the predicate `Engine._queue_is_active` below evaluates once
    per scheduling decision (never threaded through as extra engine state, per this task's own
    brief). `True` when any of:

    - a `job` row `queued` or `running` for one of this queue's items
    - an item in a transient lifecycle state (`_TRANSIENT_ACTIVE_STATES`)
    - an item held at the settle gate (`REMOTE_ONLY`/`substate='settling'`) -- the "arriving"
      case the user named explicitly
    - an item `PostprocessPipeline.in_flight_item_ids()` (passed in as `postprocess_in_flight_ids`
      -- an in-memory set, not a table) is currently working on

    One query -- a `UNION` of indexed `EXISTS`-shaped selects (`idx_item_queue_id`,
    `idx_item_state`, `idx_job_state`, `idx_job_item_id` already cover every branch) -- not four
    round trips. A free function, not an `Engine` method, so it's unit-testable against a bare
    `aiosqlite.Connection` and a hand-built `item`/`job` fixture, no running engine required.
    """
    in_flight = sorted(postprocess_in_flight_ids)
    in_flight_clause = (
        " UNION SELECT 1 FROM item WHERE item.queue_id = ? AND item.id IN "
        f"({','.join('?' for _ in in_flight)})"
        if in_flight
        else ""
    )
    # Positional params, in the exact order their `?` placeholders appear above: job/queued
    # clause, transient-state clause (queue_id then the state list), settling clause, and --
    # only when non-empty -- the in-flight clause (queue_id then the id list).
    params: list[Any] = [
        queue_id,
        queue_id,
        *_TRANSIENT_ACTIVE_STATES,
        queue_id,
    ]
    if in_flight:
        params.append(queue_id)
        params.extend(in_flight)
    cursor = await db.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM job JOIN item ON item.id = job.item_id"
        "  WHERE item.queue_id = ? AND job.state IN ('queued', 'running')"
        "  UNION"
        "  SELECT 1 FROM item WHERE item.queue_id = ? AND item.state IN "
        f"({','.join('?' for _ in _TRANSIENT_ACTIVE_STATES)})"
        "  UNION"
        "  SELECT 1 FROM item WHERE item.queue_id = ? AND item.state = 'REMOTE_ONLY' "
        "AND item.substate = 'settling'"
        f"{in_flight_clause}"
        ")",
        params,
    )
    row = await cursor.fetchone()
    return bool(row[0])


# 2026-08-13 (prompts/2026-08-13-vanished-rows-should-leave-the-tree.md): the two states
# `_persist`'s vanished-from-both-trees sweep can land on that mean "gone for good, in neither
# tree" -- DESIGN.md §3.2's own "kept as history" states. Once the sweep resolves a row to one
# of these, that row must stop being **published** (though it keeps being *written* -- see
# `_persist`'s use of this set, below). Same pairing as `core/itemview.py`'s private
# `_LOCAL_REMOVED_STATES`, duplicated rather than imported: that constant is itemview's own
# "always render dark" rule, a different question that happens to share the same two states.
_TERMINAL_REMOVED_STATES = frozenset({"REMOVED_LOCAL", "REMOVED_BOTH"})


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def effective_scan_interval(
    queue_scan_interval_s: float | None, site_default_s: float
) -> float | None:
    """Resolve one queue's `path_queue.scan_interval_s` (migration 009,
    prompts/done/2026-08-12-per-queue-scan-interval.md) against the site-wide default.

    Three states on the stored column, not two: `None` (the column's own NULL) means "this
    queue has no opinion -- use `site_default_s`," a positive number is a literal per-queue
    interval in seconds, and `0` (or anything `<= 0`, though the API/DB CHECK only ever lets
    `0` or a positive value through) means **on-demand only** -- returned here as `None` too,
    but a *result* `None` means something different from an *input* `None`: no timer will ever
    fire for this queue again, versus "substitute the site default and treat that as the
    timer." `core/engine.py`'s scheduling code below only ever sees the return value, so both
    input shapes collapsing to one "never" output is exactly the simplification that matters to
    the caller -- the loop does not need to know *why* a queue has no timer, only that it
    doesn't. Pure and unit-tested on its own (`tests/test_engine_scan_cadence.py`) because this
    is the one piece of the per-queue-cadence feature that is easy to get quietly backwards.
    """
    if queue_scan_interval_s is None:
        return site_default_s
    if queue_scan_interval_s <= 0:
        return None
    return queue_scan_interval_s


@dataclass(frozen=True)
class QueueConfig:
    id: int
    host_id: int
    name: str
    remote_path: str
    local_path: str
    staging_path: str | None
    enabled: bool
    sync_mode: str
    # Phase 4 (DESIGN.md §4.7): per-queue, default off (migration 002 adds the column with
    # DEFAULT 0; migration 001 already defaulted auto_queue_enabled to 0). A capability that
    # turns itself on for an existing queue is a bug -- see docs/decisions.md.
    auto_queue_enabled: bool = False
    auto_queue_patterns_only: bool = False
    # Migration 009: `None` (every existing row -- `ADD COLUMN` with no `DEFAULT` leaves it
    # NULL) means "use the site-wide `scan_interval_s` default," resolved through
    # `effective_scan_interval` above -- never read as a literal 0/None interval directly here.
    scan_interval_s: float | None = None
    # Migration 017 ("folder prefix during transfer", `core/download_prefix.py`). Both `None` =
    # inherit the site-wide `DownloadPrefixSettings` field, resolved independently
    # (`download_prefix.resolve_for_queue`) -- see `_active_download_prefixes` below, the only
    # reader of these two on this dataclass.
    download_prefix_enabled: bool | None = None
    download_prefix: str | None = None


async def load_host_config(db: aiosqlite.Connection, config_dir: str) -> HostConfig | None:
    """Load the (single, v1) host row and decrypt its password / pasted key if applicable.

    Returns `None` if no host is configured yet. Decryption failure (DESIGN.md §8's
    "credentials need re-entry") is *not* raised here — it's deferred until something
    actually needs the credential (`core/remote.py` raises `DecryptionNeededError` for a
    password at connect time; a pasted key that fails here sets `credentials_need_reentry`
    the same way a password does), so a key-auth or agent-auth host isn't blocked by an
    unrelated password field, and vice versa.
    """
    cursor = await db.execute(
        "SELECT id, address, port, username, auth_method, key_path, password_enc, "
        "ssh_key_enc, known_hosts_policy FROM host ORDER BY id LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    password: str | None = None
    ssh_key: str | None = None
    credentials_need_reentry = False
    if row["auth_method"] == "password" and row["password_enc"]:
        try:
            password = decrypt_secret(config_dir, row["password_enc"])
        except DecryptionError as exc:
            logger.warning(
                "host %s: password does not decrypt (credentials need re-entry): %s", row["id"], exc
            )
            password = None
            credentials_need_reentry = True
    elif row["auth_method"] == "key" and row["ssh_key_enc"]:
        try:
            ssh_key = decrypt_secret(config_dir, row["ssh_key_enc"])
        except DecryptionError as exc:
            logger.warning(
                "host %s: pasted key does not decrypt (credentials need re-entry): %s",
                row["id"],
                exc,
            )
            ssh_key = None
            credentials_need_reentry = True

    return HostConfig(
        id=row["id"],
        address=row["address"],
        port=row["port"],
        username=row["username"],
        auth_method=row["auth_method"],
        key_path=row["key_path"],
        password=password,
        ssh_key=ssh_key,
        known_hosts_policy=row["known_hosts_policy"],
        credentials_need_reentry=credentials_need_reentry,
    )


async def load_queues(db: aiosqlite.Connection) -> list[QueueConfig]:
    cursor = await db.execute(
        "SELECT id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode, "
        "auto_queue_enabled, auto_queue_patterns_only, scan_interval_s, "
        "download_prefix_enabled, download_prefix FROM path_queue ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [
        QueueConfig(
            id=row["id"],
            host_id=row["host_id"],
            name=row["name"],
            remote_path=row["remote_path"],
            local_path=row["local_path"],
            staging_path=row["staging_path"],
            enabled=bool(row["enabled"]),
            sync_mode=row["sync_mode"],
            auto_queue_enabled=bool(row["auto_queue_enabled"]),
            auto_queue_patterns_only=bool(row["auto_queue_patterns_only"]),
            scan_interval_s=row["scan_interval_s"],
            # Nullable-for-inherit (migration 017) -- unlike `auto_queue_enabled` above, `None`
            # here is a real, distinct value (`bool(row[...])` would collapse it to `False`,
            # silently turning "inherit" into an explicit override the moment a queue is loaded).
            download_prefix_enabled=(
                None
                if row["download_prefix_enabled"] is None
                else bool(row["download_prefix_enabled"])
            ),
            download_prefix=row["download_prefix"],
        )
        for row in rows
    ]


def diff_nodes(
    old: dict[str, ItemView], new: dict[str, ItemView]
) -> tuple[list[ItemView], list[str]]:
    """The WebSocket delta fix (DESIGN.md §2/§9; docs/decisions.md's phase 2 entry flagged
    this shape as scoped-down and said phase 3 shouldn't inherit it by default).

    Phase 2 published one full-tree `queue_snapshot` — every node, every scan — because
    nothing else existed yet and a 30s cadence made it cheap. Phase 3a's ~1 Hz progress
    sampler makes that shape actively wrong: a queue holding a few thousand files would
    re-serialize and re-send the entire tree to every connected browser every second.

    Both sides are `core/itemview.py` projections of the persisted `item` rows — not
    `core/reconcile.py`'s structural nodes, which is what this diffed until the two were
    reconciled (see `core/itemview.py`'s module docstring). Diffing what was actually stored
    is what makes an item whose *effective* state changed while its structural node did not —
    a §7.3 grace period expiring into `REMOVED_LOCAL`, a post-processing outcome reasserted
    over a fresh `DOWNLOADED` — visible to the diff at all.

    An `ItemView` is a plain dict of scalars, so "changed" is exactly the equality check
    below — no field-by-field bookkeeping needed. A node whose `rel_path` didn't exist in
    `old`, or whose value differs from `old`'s, is "changed"; a `rel_path` present in `old`
    but absent from `new` is "removed". This is a pure function precisely so it's
    unit-testable without a running engine or a live SSH connection — see
    `tests/test_ws_deltas.py`, which is also where "the payload doesn't scale with tree size"
    is proven, not just asserted.
    """
    changed = [node for rel_path, node in new.items() if old.get(rel_path) != node]
    removed = [rel_path for rel_path in old if rel_path not in new]
    return changed, removed


def build_scan_counts_predicate(
    pattern_predicate: patterns.CountsPredicate, deleted_archive_paths: frozenset[str]
) -> patterns.CountsPredicate:
    """One queue's real `counts_predicate` for `reconcile()` -- everything `scan_queue` hands
    it, composed. Two sources feed the identical seam
    (`core/reconcile.py`'s `CountsPredicate` -- a remote file counts toward completeness unless
    something says otherwise) for two different reasons a file shouldn't count:

    - `pattern_predicate` (`core/patterns.py.build_counts_predicate`): a `file_exclude` pattern
      matched it (DESIGN.md §3.2 rule 8, §4.7).
    - `deleted_archive_paths` (2026-08-13,
      `prompts/2026-08-13-delete-archives-after-extract.md`): this codebase deleted it itself,
      after a successful extraction (`core/local_delete.py.delete_extracted_archives`). Without
      this, an item whose spent `.rar`/`.r00`/... volumes were removed reads `local < remote` on
      the very next scan -> `PARTIAL` (§3.2 rule 2), which beats any post-processing outcome
      (rule 9) and would re-fetch/re-extract/re-delete it every scan interval, forever -- the
      same shape as the `REMOVED_LOCAL` bug shipped and reverted the same night in `6d3bd95`.

    Deliberately **one predicate, not a second completeness rule**: both a `rel_path` matched
    by a pattern and one this codebase deleted end up marked `EXCLUDED` by `reconcile()`
    through the exact same branch, so a directory's vacuous-`DOWNLOADED`-when-everything-
    excluded handling (§3.2 rule 8, `core/reconcile.py`'s `remote_file_totals` split) applies
    identically to both causes without `reconcile.py` itself needing to know there are two.

    A free-standing function (not inlined in `scan_queue`) so the composition itself --
    "either source excludes it, in either order, and only membership matters" -- is
    unit-testable without a database, a filesystem, or a running `Engine`.
    """

    def predicate(rel_path: str, entry: RemoteEntry) -> bool:
        if rel_path in deleted_archive_paths:
            return False
        return pattern_predicate(rel_path, entry)

    return predicate


class Engine:
    """Owns the scan loop, the current in-memory model (one reconciled tree per queue), and
    the pooled remote connection. One instance lives on `app.state.engine` for the process
    lifetime (DESIGN.md §2).
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config_dir: str,
        events: EventBus,
        scan_interval_s: float = DEFAULT_SCAN_INTERVAL_S,
        autoqueue: AutoQueue | None = None,
    ) -> None:
        self.db = db
        self.config_dir = config_dir
        self.events = events
        self.scan_interval_s = scan_interval_s
        self.pool = RemoteConnectionPool(Path(config_dir))
        # Phase 4 (DESIGN.md §4.7): injected rather than constructed here, the same shape
        # `core/queue.py`'s `host_provider` uses — `AutoQueue` needs `TransferQueue.
        # enqueue_item`, which doesn't exist yet when `Engine` is constructed in `main.py`'s
        # lifespan. `None` (the default, and what every existing test still constructs)
        # means auto-queue is simply never evaluated -- not a crash, not a silent no-op that
        # looks like a bug, just "this capability isn't wired up."
        self.autoqueue = autoqueue
        # Phase 5's pipeline (DESIGN.md §6), set as a plain attribute after construction for
        # the same reason `core/queue.py` takes it that way: `main.py`'s lifespan can't build
        # it until `Engine.pool` exists. Read for one thing only -- `in_flight_item_ids()`,
        # in `_protected_rel_paths` below. `None` (every existing test's default) simply means
        # no item is ever reported as mid-postprocessing, which is exactly right for an
        # engine running without a pipeline attached.
        self.postprocess: Any = None
        # 2026-08-13 (`prompts/2026-08-13-delete-state-truthfulness.md`): the identical seam
        # for `core/local_delete.py.DeleteInFlight` -- a `main.py`-can't-construct-it-yet plain
        # attribute, read the same single way (`in_flight_item_ids()`) in
        # `_protected_rel_paths` below, so a scan pass can't recompute a row's structural state
        # while `delete_local()` is still actually removing its files. `None` (every existing
        # test's default) means no item is ever reported as mid-delete, unchanged from before
        # this task.
        self.delete_in_flight: Any = None

        # rel_path -> the projection of that item's persisted row, per queue (DESIGN.md §2).
        # A *cache of the `item` table*, refreshed from it after every persist — never the
        # structural reading `core/reconcile.py` produced on the way in. See
        # `core/itemview.py` for why the distinction is the whole point.
        self.models: dict[int, dict[str, ItemView]] = {}
        self.queue_meta: dict[int, QueueConfig] = {}
        self.scan_errors: dict[int, str | None] = {}
        # A *soft* per-queue note (DESIGN.md §5) — set when a scan completed but skipped one
        # or more unreadable remote subtrees (core/remote.py's scan-abort fix) rather than
        # failing outright. Distinct from `scan_errors`, which means the whole scan failed.
        self.scan_warnings: dict[int, str | None] = {}
        self.last_scan_at: dict[int, str | None] = {}
        # The mount sentinel gate's current reading per queue (DESIGN.md §7.3, required here
        # per docs/decisions.md) -- exposed to the API/UI so "why isn't auto-queue doing
        # anything" is answerable without reading a log.
        self.mount_ok: dict[int, bool] = {}

        # Per-queue next-due bookkeeping for the multi-cadence loop below
        # (prompts/done/2026-08-12-per-queue-scan-interval.md). `time.monotonic()`-comparable
        # epoch, never wall-clock -- a clock step must not skip or double a scan. A queue
        # absent from this dict has never completed a pass and is therefore due immediately
        # (matches every prior phase's behavior: the very first `scan_all()` call, with this
        # dict empty, scans every enabled queue exactly as it always has). `math.inf` is the
        # explicit "never due on a timer" marker for a queue whose `effective_scan_interval`
        # resolved to `None` (the "none" / on-demand-only choice) -- kept distinct from simply
        # never appearing in the dict, which instead means "not yet scanned even once."
        self._next_due: dict[int, float] = {}
        # Companion to `_next_due`, for the ~5s active-only local pass
        # (`prompts/2026-08-14-adaptive-scan-cadence-when-active.md`, `ACTIVE_SCAN_INTERVAL_S`)
        # layered on top of it -- same `time.monotonic()`-comparable / `math.inf`-means-never
        # convention, set alongside `_next_due` every time a *full* scan runs (see `scan_all`)
        # and re-set on its own between full scans (see `_schedule_next_local`). A forced pass
        # restarts this clock too, for the same reason `_next_due`'s own comment gives.
        self._next_local_due: dict[int, float] = {}
        # This queue's remote tree as of its last full scan (`scan_queue`, refreshed there on
        # every successful `self.pool.scan`, including a partial-scan warning -- never left
        # stale across one) -- what a local-only pass (`_scan_queue_local_only`) reconciles a
        # fresh local walk against instead of re-reading the seedbox. Absent entirely for a
        # queue that has never completed a full scan; that absence is the guard
        # `_scan_queue_local_only`'s caller checks before ever calling it (a local-only pass
        # against no remote tree at all would read every local file as `LOCAL_ONLY` and, worse,
        # feed `_persist`'s vanished-row sweep an empty remote for every previously-tracked
        # path -- see this task's decisions.md entry).
        self._cached_remote_tree: dict[int, dict[str, RemoteEntry]] = {}
        # Set by `request_rescan()`, consumed and cleared by `_loop` on its next wake: a forced
        # pass scans every enabled queue regardless of its own due time (the same "Rescan now" /
        # config-change semantics this loop has always had), and *also* restarts every scanned
        # queue's own clock from that pass's completion -- a forced rescan is not free for a
        # slow-cadence queue, it is a real scan, and its next natural due time moves accordingly
        # rather than firing again moments later.
        self._force_full = False

        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-engine-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.pool.close()

    def request_rescan(self) -> None:
        """Trigger an immediate, full scan pass rather than waiting for any queue's own
        interval — used by the on-demand rescan API, *Test connection* succeeding after a
        config change, and every settings write that can change what a scan would see.
        """
        self._force_full = True
        self._wake.set()

    def forget_rel_paths(self, queue_id: int, rel_paths: Iterable[str]) -> None:
        """Evict `rel_paths` from `self.models[queue_id]` and tell every connected browser they
        are gone — the one piece of `core/local_delete.py.reset_item`/`reset_queue`/
        `reset_by_pattern` (2026-08-13, `prompts/2026-08-13-reset-item-tracking.md`) that module
        cannot do itself, because `self.models` is this class's own private cache and nothing
        outside it may write to it.

        **Why this exists at all.** Every other writer of `item` rows publishes through a scan
        (`scan_queue` above, via `_persist`/`_project`/`diff_nodes`) or keeps the row alive
        (`delete_local`, which updates `state` but never removes the row — its own module
        docstring's "Row lifetime" paragraph). A reset is the first thing in this codebase that
        deletes an `item` row outright while the process keeps running, and it does so from a
        plain SQL `DELETE`, entirely outside a scan pass. Without this call, `self.models` keeps
        serving the pre-reset row forever: if nothing remote or local exists at that path anymore
        (the "fully forget this, it's gone" case), no future scan ever revisits it — reconcile
        only produces entries for paths present on *some* side — so the stale entry would be a
        permanent ghost row on the Files page, contradicting "genuinely gone" the whole feature
        promises. If the path *does* still exist on either side, the next scan would eventually
        self-correct this on its own (a fresh `item` row diffs as "changed" against the stale
        cached one) — but "eventually" is up to one whole `scan_interval_s` away, and the API
        layer (`api/jobs.py`) already calls `request_rescan()` right after this for exactly that
        gap, the same "retroactive" idiom pattern create/update/delete already use.

        Reuses `queue_delta`'s exact wire shape (`changed=[]`, `removed=rel_paths`) rather than
        inventing a new message type — `hooks/useLiveModel.ts` already knows how to drop rows
        named in `removed` (the ordinary "this node left the tree" case `diff_nodes` produces
        every scan), so there is nothing new for the frontend to learn.
        """
        nodes = self.models.get(queue_id)
        if not nodes:
            return
        removed = [rel_path for rel_path in rel_paths if nodes.pop(rel_path, None) is not None]
        if not removed:
            return
        meta = self.queue_meta.get(queue_id)
        self.events.publish(
            {
                "type": "queue_delta",
                "queue_id": queue_id,
                "queue_name": meta.name if meta else "",
                "changed": [],
                "removed": removed,
                "scanned_at": self.last_scan_at.get(queue_id),
                "warning": self.scan_warnings.get(queue_id),
            }
        )

    async def _loop(self) -> None:
        while True:
            forced = self._force_full
            self._force_full = False
            try:
                await self.scan_all(force=forced)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("engine scan cycle failed")
            # `None` here means "wait indefinitely for `request_rescan()`" -- every enabled
            # queue is on-demand-only, or there are no enabled queues at all. `asyncio.wait_for`
            # treats `timeout=None` as "no timeout," not "timeout immediately."
            timeout = self._next_wake_delay()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass
            self._wake.clear()

    def _next_wake_delay(self) -> float | None:
        """How long `_loop` should sleep before its next pass: the shortest remaining time
        across every *enabled* queue's own next-due -- **both** clocks, `_next_due` (full scans)
        and `_next_local_due` (the ~5s active-only local pass, `prompts/2026-08-14-adaptive-
        scan-cadence-when-active.md`) -- clamped so it is never negative (an overrunning scan
        can only ever push a queue's own next-due into the future -- see `scan_all` -- but this
        still guards the general case defensively). A queue not yet due-tracked on either clock
        (never scanned at all) is due right now, so its presence alone collapses the whole
        result to `0.0` without needing to compare a timestamp -- in practice this only fires
        for a queue `scan_all` hasn't processed even once yet, since both clocks are always set
        together on a queue's first pass (see `scan_all`). A clock reading `math.inf` (never on
        a timer -- see `_next_due`'s own comment) is excluded from the min() below; if every
        enabled queue's every clock is like that, or there are no enabled queues, there is
        nothing to wait *for* on a timer and this returns `None`.
        """
        now = time.monotonic()
        delays: list[float] = []
        for q in self.queue_meta.values():
            if not q.enabled:
                continue
            for due_times in (self._next_due, self._next_local_due):
                due_at = due_times.get(q.id)
                if due_at is None:
                    return 0.0
                if due_at == math.inf:
                    continue
                delays.append(due_at - now)
        if not delays:
            return None
        return max(0.0, min(delays))

    def _schedule_next(self, q: QueueConfig, *, now: float) -> None:
        """Set queue `q`'s next-due from `now` (its own scan's completion time, not the batch
        start -- see `scan_all`), so an overrun costs only that queue's own next interval and
        can never make it due again before the loop has even finished this pass, let alone
        while a scan of it is still in flight (`_loop`/`scan_all` are one serial task; nothing
        in this class ever awaits two `scan_queue` calls for the same queue concurrently).

        **Unaffected by activity** (`queue_is_active`) -- this is the *full*-scan clock, and
        "the remote keeps its configured cadence" regardless of how busy the queue is
        (`prompts/2026-08-14-adaptive-scan-cadence-when-active.md`'s explicit decision, restoring
        DESIGN.md §5's original two-cadence shape -- see docs/decisions.md). `_schedule_next_local`
        is the companion that *does* tighten while active.
        """
        interval = effective_scan_interval(q.scan_interval_s, self.scan_interval_s)
        self._next_due[q.id] = now + interval if interval is not None else math.inf

    def _schedule_next_local(self, q: QueueConfig, *, now: float) -> None:
        """Set queue `q`'s next *local-check* due time -- the companion clock
        `_scan_queue_local_only`'s caller (`scan_all`) uses to decide when to next re-evaluate
        `queue_is_active` and, if active, run another local-only pass. Same completion-time
        scheduling discipline as `_schedule_next` (an overrunning local-only pass costs itself a
        longer effective gap, never a stacked second one), and the same `math.inf`-means-never
        convention when the underlying full interval is on-demand-only
        (`resolve_active_check_interval(None) is None` -- a queue with no timer must not gain
        one just by becoming active).
        """
        interval = resolve_active_check_interval(
            effective_scan_interval(q.scan_interval_s, self.scan_interval_s)
        )
        self._next_local_due[q.id] = now + interval if interval is not None else math.inf

    def _is_due(self, queue_id: int, now: float) -> bool:
        due_at = self._next_due.get(queue_id)
        return due_at is None or now >= due_at

    def _is_local_due(self, queue_id: int, now: float) -> bool:
        due_at = self._next_local_due.get(queue_id)
        return due_at is None or now >= due_at

    async def _queue_is_active(self, queue_id: int) -> bool:
        """`queue_is_active` (module-level, free function -- see its own docstring for why),
        supplying it this instance's live `postprocess.in_flight_item_ids()` -- `frozenset()`
        when no pipeline is attached, matching every other reader of that attribute in this
        class.
        """
        in_flight = (
            frozenset(self.postprocess.in_flight_item_ids()) if self.postprocess else frozenset()
        )
        return await queue_is_active(self.db, queue_id, in_flight)

    async def scan_all(self, *, force: bool = False) -> None:
        """One pass over every configured queue. `force=True` (only ever set by
        `request_rescan()`, via `_loop`) scans every *enabled* queue unconditionally, the
        original "one global interval" behavior; `force=False` (the timer path) scans only the
        queues whose own `_is_due` says are due, which is the entire point of a per-queue
        cadence -- see `_loop`'s docstring-equivalent comments and
        `tests/test_engine_scan_cadence.py`.

        **A queue not due for a full scan may still get a local-only pass**
        (`prompts/2026-08-14-adaptive-scan-cadence-when-active.md`): when its `_next_local_due`
        clock is due, this checks `queue_is_active` (one cheap query, no SSH) and, only if both
        that *and* a cached remote tree from a prior full scan exist, runs
        `_scan_queue_local_only`. The local clock is always rescheduled when checked -- even
        when guarded out or found idle -- so this never busy-spins waiting on a queue that never
        becomes active or never gets its first successful full scan.
        """
        host = await load_host_config(self.db, self.config_dir)
        queues = await load_queues(self.db)
        self.queue_meta = {q.id: q for q in queues}

        # A deleted queue's leftover due-time (and cached remote tree) must not linger forever
        # -- harmless (never read again once the id is gone from `queue_meta`), but unbounded
        # growth in a long-running process is still worth not doing.
        current_ids = {q.id for q in queues}
        for stale_id in [qid for qid in self._next_due if qid not in current_ids]:
            del self._next_due[stale_id]
        for stale_id in [qid for qid in self._next_local_due if qid not in current_ids]:
            del self._next_local_due[stale_id]
        for stale_id in [qid for qid in self._cached_remote_tree if qid not in current_ids]:
            del self._cached_remote_tree[stale_id]

        now = time.monotonic()
        for q in queues:
            if not q.enabled:
                continue
            if force or self._is_due(q.id, now):
                await self.scan_queue(q, host)
                # Scheduled from *this queue's own* completion time, not the shared `now` above
                # or the batch's start -- see `_schedule_next`'s docstring for why that's what
                # actually prevents an overrunning scan from stacking a second one of the same
                # queue. Both clocks restart here -- a full scan is strictly fresher than any
                # local-only pass could be, so it absorbs a pending local check the same way a
                # forced full scan already absorbs a pending natural one.
                completion = time.monotonic()
                self._schedule_next(q, now=completion)
                self._schedule_next_local(q, now=completion)
                continue
            if not self._is_local_due(q.id, now):
                continue
            # Guard (prompts/2026-08-14-adaptive-scan-cadence-when-active.md): never reconcile
            # a local-only pass against a remote tree this queue hasn't actually observed yet --
            # see `_cached_remote_tree`'s own comment for what an empty remote would do to
            # `_persist`'s vanished-row sweep. `queue_is_active` is a real query; skip it
            # entirely when the guard alone already rules out doing any work this tick.
            if q.id in self._cached_remote_tree and await self._queue_is_active(q.id):
                await self._scan_queue_local_only(q)
            self._schedule_next_local(q, now=time.monotonic())

    async def _active_download_prefixes(self, q: QueueConfig) -> tuple[str, ...]:
        """Every directory-name prefix a local scan of `q` must filter out right now
        ("folder prefix during transfer," `core/download_prefix.py`) -- the resolved
        site/queue prefix, *if* currently enabled, unioned with every distinct
        `item.pending_download_prefix` still on record for this queue. The union, not just the
        resolved value, is what keeps a *stale* prefix from orphaning a directory: an item
        spawned while the site or queue prefix said `.downloading-` keeps writing into
        `.downloading-<name>` for that job's whole lifetime even if the setting is edited (or
        turned off, or a `STOPPED` item just sits there) before it finishes --
        `core/queue.py._spawn_decision` never re-derives the prefix for an item that already has
        one recorded, and this is the matching read side: a scan must keep skipping whatever
        directory name is *physically* in use, not merely whatever today's settings resolve to.
        `pending_download_prefix` is cleared the moment `core/queue.py._reap_one` renames a
        directory back to its real name, so a queue with nothing currently in flight under an
        old prefix costs this method nothing beyond the one indexed query below.
        """
        site = await download_prefix.load_download_prefix_settings(self.db)
        enabled, prefix = download_prefix.resolve_for_queue(
            q.download_prefix_enabled, q.download_prefix, site
        )
        prefixes = {prefix} if enabled else set()
        cursor = await self.db.execute(
            "SELECT DISTINCT pending_download_prefix FROM item "
            "WHERE queue_id = ? AND pending_download_prefix IS NOT NULL",
            (q.id,),
        )
        rows = await cursor.fetchall()
        prefixes.update(row["pending_download_prefix"] for row in rows)
        return tuple(prefixes)

    async def scan_queue(self, q: QueueConfig, host: HostConfig | None) -> None:
        try:
            if host is None:
                raise RemoteScanError("no host configured")
            # DESIGN.md §8: "mark the host credentials need re-entry... rather than crashing
            # or retrying." Raising here (instead of letting `self.pool.scan` attempt the
            # connection) means this queue's scan fails with one clean, stable message every
            # pass rather than actually opening a doomed SSH connection every 30s and getting
            # a `DecryptionNeededError` two frames deeper each time -- same observable
            # end state (the queue's `scan_errors` entry), but no retried connection attempt.
            if host.credentials_need_reentry:
                raise RemoteScanError(
                    "credentials need re-entry -- update the seedbox password in "
                    "Settings -> Connection"
                )
            remote_tree, scan_warning = await self.pool.scan(host, q.remote_path)
            # Refreshed here, unconditionally, on every successful remote read -- including one
            # that comes back with a `scan_warning` (a partial scan still returns a real tree,
            # just possibly missing an unreadable subtree; see `core/remote.py.interpret_
            # primary_scan_result`). Never touched on the exception path below -- a queue whose
            # remote scan fails outright keeps its last-known-good cached tree rather than losing
            # it, the same "don't invent data, don't discard good data either" shape every other
            # failure path in this method already has. `_scan_queue_local_only` is this cache's
            # only reader.
            self._cached_remote_tree[q.id] = remote_tree
            prefixes = await self._active_download_prefixes(q)
            local_tree = local_scan.scan_local(q.local_path, extra_dir_prefixes=prefixes)

            # The mount sentinel (DESIGN.md §7.3, required starting this phase — see
            # docs/decisions.md): written once the local root is confirmed present and
            # writable, checked on every pass regardless. `self.mount_ok` is read by
            # `AutoQueue.on_scan` below and exposed to the API/UI.
            mount_sentinel.write_if_needed(q.local_path)
            self.mount_ok[q.id] = mount_sentinel.check(q.local_path)

            # DESIGN.md §4.7/§3.2 rule 8: the same compiled file_exclude set that will build
            # lftp's --exclude-glob arguments (core/queue.py) also tells the reconciler what
            # a directory is supposed to contain. Recompiled fresh every scan (cheap — see
            # core/patterns.py's own docstring) so a pattern edit takes effect on the very
            # next pass, not just for items scanned after it.
            compiled = await patterns.compiled_for_queue(self.db, q.id)
            pattern_predicate = patterns.build_counts_predicate(compiled)
            # 2026-08-13 (prompts/2026-08-13-delete-archives-after-extract.md): the second
            # source `build_scan_counts_predicate` folds in -- see that function's docstring.
            deleted_archive_paths = await local_delete.load_deleted_archive_paths(self.db, q.id)
            counts_predicate = build_scan_counts_predicate(pattern_predicate, deleted_archive_paths)
            nodes = reconcile(remote_tree, local_tree, counts_predicate=counts_predicate)

            # The settle gate (prompts/open-issues.md #2, `core/settle.py`): one fingerprint
            # per top-level item, computed straight from this pass's own remote tree -- no
            # extra I/O, the same tree `reconcile` above just consumed. `_persist` is what
            # turns this into a settled/unsettled verdict and decides what (if anything) to
            # override; this call only produces the raw per-item numbers.
            fingerprints = settle.compute_fingerprints(remote_tree)

            # Persist first, then read back what was actually stored, then diff *that*
            # (DESIGN.md §2/§9; `core/itemview.py`). `_persist` is where an item's state is
            # really decided — job-lifecycle protection, §6's post-processing precedence,
            # §7.3's grace period, the settle gate above — so diffing `nodes` here, as this
            # did until the two views were reconciled, published a state the database
            # disagreed with. The order is the invariant: nothing goes on the wire that
            # wasn't read back out of `item`.
            written = await self._persist(
                q.id,
                nodes,
                fingerprints=fingerprints,
                partial_scan=bool(scan_warning),
                deleted_archive_paths=deleted_archive_paths,
            )
            published = await self._project(q.id, written)

            old_nodes = self.models.get(q.id, {})
            changed, removed = diff_nodes(old_nodes, published)
            self.models[q.id] = published

            self.scan_errors[q.id] = None
            self.scan_warnings[q.id] = scan_warning
            self.last_scan_at[q.id] = _now_iso()
            # The delta itself (DESIGN.md §2/§9): only the rows that changed since the last
            # scan, not the whole tree — see `diff_nodes`'s docstring. A full snapshot is
            # sent exactly once per connection, by `snapshot()` below, never here.
            self.events.publish(
                {
                    "type": "queue_delta",
                    "queue_id": q.id,
                    "queue_name": q.name,
                    "changed": changed,
                    "removed": removed,
                    "scanned_at": self.last_scan_at[q.id],
                    "warning": scan_warning,
                }
            )
            # The completion signal a client can actually wait on (DESIGN.md §2/§9; see
            # docs/decisions.md): "Rescan now" used to fake completion with a bare 1s
            # `setTimeout`, which was simply wrong on any tree big enough to take longer than
            # that. `queue_delta` above isn't a substitute -- it's published on every pass
            # regardless of outcome *except* this one never fires on a failed pass, which is
            # exactly the case a spinning button most needs a signal for. Deliberately its own
            # message rather than a field bolted onto `queue_delta`, so a listener doesn't have
            # to infer "this pass is over" from a message shape meant for tree deltas. Same
            # fixed-size shape regardless of the outcome or tree size: four scalars, never a
            # node list.
            self.events.publish(
                {
                    "type": "scan_complete",
                    "queue_id": q.id,
                    "finished_at": self.last_scan_at[q.id],
                    "ok": True,
                    "warning": scan_warning,
                }
            )

            if self.autoqueue is not None:
                await self.autoqueue.on_scan(
                    QueueAutoConfig(
                        id=q.id,
                        local_path=q.local_path,
                        auto_queue_enabled=q.auto_queue_enabled,
                        patterns_only=q.auto_queue_patterns_only,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - recorded per-queue, never propagated
            message = str(exc)
            self.scan_errors[q.id] = message
            logger.warning("scan failed for queue %s (%s): %s", q.id, q.name, message)
            self.events.publish(
                {"type": "scan_error", "queue_id": q.id, "queue_name": q.name, "message": message}
            )
            # Published on the failure path too, not just success (see the success-path
            # comment above) -- a scan that errors out must still tell a waiting "Rescan now"
            # button the pass is over, or it spins forever. `ok: False` and no `warning`: a
            # pass that failed outright never got far enough to know whether it would also
            # have carried a partial-scan warning, and `scan_error`'s own `message` already
            # carries the failure detail.
            self.events.publish(
                {
                    "type": "scan_complete",
                    "queue_id": q.id,
                    "finished_at": _now_iso(),
                    "ok": False,
                    "warning": None,
                }
            )

    async def _scan_queue_local_only(self, q: QueueConfig) -> None:
        """The fast (~`ACTIVE_SCAN_INTERVAL_S`) pass while queue `q` is active
        (`queue_is_active`) -- `prompts/2026-08-14-adaptive-scan-cadence-when-active.md`,
        restoring the local half of DESIGN.md §5's original two-cadence design. Rescans **only**
        the local filesystem and reconciles it against `self._cached_remote_tree[q.id]`, the
        tree from this queue's last full scan -- never an SSH round trip. Callers (`scan_all`)
        must not invoke this before that cache exists; see `_cached_remote_tree`'s own comment
        for what a missing/empty remote tree would do to `_persist`'s vanished-row handling.

        **`fingerprints=None`** on the `_persist` call below -- the one line that matters most
        in this whole task. This pass never re-read the remote, so it has no fresh evidence
        about whether a top-level item is still arriving; `_persist` reads the *last-persisted*
        settle verdict to decide whether a `DOWNLOADED` reading is still gated (so an item the
        real settle gate hasn't cleared yet cannot be released early just because local bytes
        caught up to a stale cached remote total), but never advances or resets
        `item_settle` itself -- `settle.save_settle_records` is only ever called when this
        pass's own `new_settle` accumulates something, which it cannot when `fingerprints` is
        `None` (see `_persist`'s docstring). `tests/test_engine_scan_cadence.py` asserts
        `item_settle` is byte-for-byte unchanged by a local-only pass.

        No `scan_error`/`scan_complete` events -- those are the full-scan "Rescan now" button's
        completion signal (see `scan_queue`'s own comments); a background local-only heartbeat
        firing one every few seconds would be noise a client has no reason to wait on. A
        `queue_delta` still goes out, same as every full pass, so sibling-item and transient-
        state changes reach the Files page without waiting on the next full scan.
        """
        cached_remote = self._cached_remote_tree.get(q.id)
        if cached_remote is None:
            return
        try:
            prefixes = await self._active_download_prefixes(q)
            local_tree = local_scan.scan_local(q.local_path, extra_dir_prefixes=prefixes)

            # Local-only, same as `scan_queue`'s own use of this sentinel -- no SSH involved
            # either way.
            mount_sentinel.write_if_needed(q.local_path)
            self.mount_ok[q.id] = mount_sentinel.check(q.local_path)

            compiled = await patterns.compiled_for_queue(self.db, q.id)
            pattern_predicate = patterns.build_counts_predicate(compiled)
            deleted_archive_paths = await local_delete.load_deleted_archive_paths(self.db, q.id)
            counts_predicate = build_scan_counts_predicate(pattern_predicate, deleted_archive_paths)
            nodes = reconcile(cached_remote, local_tree, counts_predicate=counts_predicate)

            written = await self._persist(
                q.id,
                nodes,
                fingerprints=None,
                partial_scan=False,
                deleted_archive_paths=deleted_archive_paths,
            )
            published = await self._project(q.id, written)

            old_nodes = self.models.get(q.id, {})
            changed, removed = diff_nodes(old_nodes, published)
            self.models[q.id] = published

            self.last_scan_at[q.id] = _now_iso()
            self.events.publish(
                {
                    "type": "queue_delta",
                    "queue_id": q.id,
                    "queue_name": q.name,
                    "changed": changed,
                    "removed": removed,
                    "scanned_at": self.last_scan_at[q.id],
                    "warning": self.scan_warnings.get(q.id),
                }
            )

            if self.autoqueue is not None:
                await self.autoqueue.on_scan(
                    QueueAutoConfig(
                        id=q.id,
                        local_path=q.local_path,
                        auto_queue_enabled=q.auto_queue_enabled,
                        patterns_only=q.auto_queue_patterns_only,
                    )
                )
        except Exception:  # noqa: BLE001 - one bad local-only tick must not kill the loop
            logger.exception("local-only scan failed for queue %s (%s)", q.id, q.name)

    async def _protected_rel_paths(self, queue_id: int) -> set[str]:
        """Items whose `state` this scan pass must **not** overwrite — DESIGN.md never says
        who wins when a periodic rescan's structural state (REMOTE_ONLY/PARTIAL/DOWNLOADED,
        computed fresh from remote-vs-local bytes on every pass, §3.2) disagrees with a
        job-lifecycle state (QUEUED/DOWNLOADING/STOPPED/FAILED) `core/queue.py` set. Left
        unresolved, the next 30s scan after a manual queue/stop silently reverts it — e.g. a
        `STOPPED` item with a still-partial file reads as `PARTIAL` again on the very next
        scan, which is indistinguishable from "not stopped" and defeats §4.6's suppression
        rule the moment it matters.

        Smallest reasonable call, surfaced rather than silently decided: `core/queue.py` owns
        `state` for any item with a `queued`/`running` job, or with `auto_queue_suppressed`
        set (STOPPED/FAILED) — this scan updates their size/mtime columns for display but
        leaves `state` alone. Everything else still gets the structural state computed above;
        in particular a job's own success path (`core/queue.py._reap_one`) clears
        `auto_queue_suppressed` and sets `DOWNLOADED` itself, so this scan is free to confirm
        it on the very next pass rather than fighting over it.

        **Extended for phase 5's post-processing states (§3.2, §6), which did not exist when
        the paragraph above was written.** `core/postprocess.py` is the third owner of
        `item.state`, and both halves of that ownership are deliberately *not* expressed the
        same way:

        - **Mid-run** (`VERIFYING`/`EXTRACTING`) an item is protected here — but keyed on the
          pipeline's in-memory `in_flight_item_ids()`, never on the state string. An extract
          can run for an hour, far longer than a scan interval, and must not be stomped while
          it does; but protection that outlived the worker would be a wedge with no way out,
          which is exactly the bug phase 3 hit with jobs left `running` by a restart
          (`core/queue.py._reconcile_orphaned_jobs`). Tying it to the live worker means a
          crash un-protects the item automatically and the next scan recomputes it.
        - **The outcomes** (`VERIFIED`/`CORRUPT`/`EXTRACTED`/`EXTRACT_FAILED`) are *not*
          listed here, because unconditional protection is the wrong shape for them: an
          `EXTRACTED` item whose local copy is later moved out by an importer would stay
          `EXTRACTED` forever and §3.2 rule 3's `REMOVED_LOCAL` transition would never fire
          for it again. They win over the structural state only while the content is actually
          present, which is a decision about *both* states and so lives in `_persist` below
          (`core/postprocess.py.outcome_survives_rescan`) alongside the absence half
          (`core/mount_sentinel.py.resolve_absence`).

        **A third live-worker source, added 2026-08-13** (`prompts/2026-08-13-delete-state-
        truthfulness.md`): `self.delete_in_flight` — `core/local_delete.py.delete_local()`'s
        own in-flight tracker, folded into the identical `in_flight` list below rather than a
        second clause, because it means exactly the same thing to this query as postprocess's
        own set does — "a live worker outside this scan pass is actively changing this item's
        files right now, so this scan must not race it." Without this, a large delete's
        `substate = 'removing'` marker (and the row's `state`, whatever it is mid-removal)
        could be overwritten by a scan that lands while `shutil.rmtree` is still running.
        """
        # `frozenset` -> sorted list purely so the SQL parameters are deterministic (log/test
        # readability); membership is what matters, not order.
        in_flight = set(self.postprocess.in_flight_item_ids()) if self.postprocess else set()
        if self.delete_in_flight is not None:
            in_flight |= set(self.delete_in_flight.in_flight_item_ids())
        in_flight = sorted(in_flight)
        in_flight_clause = (
            f" OR item.id IN ({','.join('?' for _ in in_flight)})" if in_flight else ""
        )
        # Descendants of an in-flight *postprocess/delete* parent, mirroring the active-job
        # descendant clause immediately below (2026-08-14,
        # prompts/done/2026-08-14-rename-after-postprocessing-not-before.md). Before that task,
        # `pending_download_prefix` was always cleared by the time an item reached DOWNLOADED --
        # postprocessing only ever ran against an already-unprefixed, fully visible tree, so a
        # child row was never at risk here. Now the rename is postprocessing's own *last* step,
        # so a top-level item can sit in VERIFYING/EXTRACTING for as long as verify/extract take
        # (measured: 7.7s for 1.7 GB) while its directory is still physically prefixed and
        # therefore still filtered out of `scan_local` entirely -- the exact shape that already
        # produced the PARTIAL/REMOTE_ONLY flicker the sibling clause below was written to fix,
        # just for a different, newly-widened window. The in-flight *item id* clause above only
        # protects the top-level row itself; without this, a child file inside a release that's
        # merely being verified or extracted (no `job` row anymore -- the transfer already
        # finished) would flip to REMOTE_ONLY on every scan that lands mid-postprocessing, exactly
        # as `core/queue.py._publish_child_progress`'s own sibling comment describes for the
        # download window.
        in_flight_descendant_clause = (
            "  OR EXISTS ("
            "    SELECT 1 FROM item AS ppparent"
            f"    WHERE ppparent.queue_id = item.queue_id AND ppparent.id IN ({','.join('?' for _ in in_flight)})"
            "      AND instr(item.rel_path, ppparent.rel_path || '/') = 1"
            "  )"
            if in_flight
            else ""
        )
        cursor = await self.db.execute(
            "SELECT item.rel_path FROM item WHERE item.queue_id = ? AND ("
            "  item.auto_queue_suppressed = 1"
            "  OR EXISTS (SELECT 1 FROM job WHERE job.item_id = item.id AND job.state IN ('queued', 'running'))"
            # **Descendants of an item with an active job are protected too** (2026-08-14).
            # A `mirror` job is tracked against the *top-level* item alone, so a child file
            # inside a downloading release has no `job` row of its own and was never caught by
            # the clause above -- yet `core/queue.py._publish_child_progress` writes exactly
            # those children's `local_size`/`state` on every progress tick. Two writers, one
            # unprotected row.
            #
            # Reported live: with "folder prefix during transfer" on, children flipped between
            # PARTIAL and REMOTE_ONLY every few seconds. The prefix makes it reproducible --
            # `scan_local(extra_dir_prefixes=...)` deliberately hides the in-flight
            # `.downloading-<name>/` tree, so the reconciler sees no local bytes for those
            # children and computes REMOTE_ONLY, which the next progress tick overwrites back
            # to PARTIAL. The 5s active-queue pass (`33db032`) turned a 30s flip into a 5s one
            # and is what made it obvious, but neither feature is the cause: the subtree simply
            # was never protected, and `core/queue.py` has owned a running job's item state
            # since phase 3 (see this method's own second paragraph).
            #
            # `instr(...) = 1` rather than `LIKE parent.rel_path || '/%'`: an exact prefix test
            # with no wildcard semantics, so a release name containing `%` or `_` can't
            # over-match (SQLite's `_` matches any single character). Bounded by the active job
            # count, which is a handful by construction.
            "  OR EXISTS ("
            "    SELECT 1 FROM item AS parent"
            "    JOIN job ON job.item_id = parent.id AND job.state IN ('queued', 'running')"
            "    WHERE parent.queue_id = item.queue_id"
            "      AND instr(item.rel_path, parent.rel_path || '/') = 1"
            "  )"
            f"{in_flight_clause}"
            f"{in_flight_descendant_clause}"
            ")",
            (queue_id, *in_flight, *in_flight),
        )
        rows = await cursor.fetchall()
        return {row["rel_path"] for row in rows}

    async def _previous_states(
        self, queue_id: int
    ) -> dict[str, tuple[str, str | None, str | None, str | None]]:
        """`rel_path -> (state, substate, first_missing_at, remote_deleted_at)` as currently
        persisted, for the grace-period decision below, (prompts/open-issues.md #2's stuck-item
        follow-up) the settle-gate release check, and (2026-08-13,
        `prompts/2026-08-13-move-mode-outcome-survives-local-only.md`)
        `postprocess.outcome_survives_rescan`'s `LOCAL_ONLY` refinement, which needs to tell "a
        never-tracked local file" from "the remote copy this codebase deleted on purpose" apart.
        One query per scan, same shape as `_protected_rel_paths`.
        """
        cursor = await self.db.execute(
            "SELECT rel_path, state, substate, first_missing_at, remote_deleted_at "
            "FROM item WHERE queue_id = ?",
            (queue_id,),
        )
        rows = await cursor.fetchall()
        return {
            row["rel_path"]: (
                row["state"],
                row["substate"],
                row["first_missing_at"],
                row["remote_deleted_at"],
            )
            for row in rows
        }

    async def _persist(
        self,
        queue_id: int,
        nodes: dict[str, ReconciledNode],
        *,
        fingerprints: dict[str, settle.Fingerprint] | None = None,
        partial_scan: bool = False,
        deleted_archive_paths: frozenset[str] = frozenset(),
    ) -> set[str]:
        """Write this pass's arbitrated state for every reconciled node, and return the
        `rel_path`s it wrote (already `to_safe_text`-ed, i.e. keyed exactly as the `item`
        table stores them) so `_project` knows which rows this scan is entitled to publish.

        `fingerprints`/`partial_scan` feed the settle gate (`core/settle.py`,
        prompts/open-issues.md #2): `fingerprints` is keyed by the *un*-safe-texted top-level
        `rel_path`, matching `node.rel_path` and the tree `settle.compute_fingerprints` was
        built from.

        **`fingerprints=None` (`prompts/2026-08-14-adaptive-scan-cadence-when-active.md`'s
        local-only pass, and every caller/test before that task) never advances or writes
        `item_settle`** -- `new_settle` can only gain an entry inside the `fingerprints is not
        None` branch below, so `save_settle_records` (only called `if new_settle:`) is a no-op
        for the whole pass. It does **not** mean "disable the gate": the *last-persisted*
        verdict (`prev_settle`, now always loaded, not conditionally) still gates a fresh
        `DOWNLOADED` reading exactly as it would on a full scan -- read-only, via the
        `fingerprints is None` branch just below -- because a pass with no new remote evidence
        must not be able to release an item the real gate hasn't actually cleared, merely
        because a stale cached remote total and a freshly-caught-up local tree happen to match
        (see this task's decisions.md entry for the scenario this closes).

        `deleted_archive_paths` (2026-08-14,
        `prompts/2026-08-14-extracted-archives-rest-as-extracted.md`) is the same set both
        callers (`scan_queue`, `_scan_queue_local_only`) already load once per pass and fold
        into `build_scan_counts_predicate` -- reused here rather than re-queried, per that
        task's own instruction. Consulted only in the "vanished from both trees" sweep below;
        see that loop's own comment for why the ordinary per-node path never needs to ask.
        """
        from lftpweb.core.util import to_safe_text

        protected = await self._protected_rel_paths(queue_id)
        previous = await self._previous_states(queue_id)
        mount_ok = self.mount_ok.get(queue_id, False)
        now = datetime.now(UTC)
        written: set[str] = set()

        settle_settings = await settle.load_settle_settings(self.db)
        # Unconditional: needed both to *advance* (fingerprints supplied) and to *read-only
        # enforce* (fingerprints is None) the gate -- see the branch below and this method's
        # own docstring.
        prev_settle = await settle.load_settle_records(self.db, queue_id)
        new_settle: dict[str, settle.SettleRecord] = {}
        # prompts/open-issues.md #2's stuck-item follow-up: `rel_path`s this pass releases from
        # a settle-gate hold straight to DOWNLOADED, so post-processing can be triggered for
        # them below -- see the long comment where this set is consumed.
        unstuck: set[str] = set()

        for node in nodes.values():
            rel_path = to_safe_text(node.rel_path)
            written.add(rel_path)

            # Settle-gate bookkeeping (`core/settle.py`): only top-level items are tracked --
            # nested children inherit their root's verdict, per the agreed design -- and only
            # when `fingerprints` was supplied at all. Computed *before* the protected check
            # below so a queued/downloading item's settle progress keeps advancing off the
            # remote tree even while its own `state` is left alone; whether it's actually
            # settled only matters once it's unprotected again (or to `core/queue.py.
            # _reap_one`'s completion gate, which reads this same table independently).
            settle_record: settle.SettleRecord | None = None
            if fingerprints is not None and "/" not in node.rel_path:
                fp = fingerprints.get(node.rel_path)
                if fp is not None:
                    settle_record = settle.advance_settle(
                        prev_settle.get(rel_path),
                        fp,
                        partial_scan=partial_scan,
                        now=now.timestamp(),
                    )
                    new_settle[rel_path] = settle_record
            elif fingerprints is None and "/" not in node.rel_path:
                # `prompts/2026-08-14-adaptive-scan-cadence-when-active.md`: a local-only pass
                # has no fresh fingerprint to advance the counter with, but the gate's
                # last-persisted verdict still applies -- read `prev_settle` straight through,
                # untouched. `new_settle` gains nothing here (this is the branch, not the write
                # side), so `item_settle` stays byte-for-byte what it was before this pass.
                settle_record = prev_settle.get(rel_path)

            if rel_path in protected:
                # 2026-08-13 (prompts/2026-08-13-delete-state-truthfulness.md, defect 2): a
                # narrow exception to "protected rows never have `state` touched," for exactly
                # one shape -- a row this codebase's own `delete_local()` already left at
                # `REMOVED_LOCAL`/`REMOVED_BOTH` (and suppressed, `deleted_local`) whose fresh
                # structural reading this pass now contradicts, because content came back on
                # one side or the other. `reconsider_removed_state` is the only thing that can
                # produce a non-`None` correction here -- every other suppressed reason
                # (`user_stopped`, `retries_exhausted`, `permanent_error`) and every other
                # `prev_state` leave this `None`, so a `STOPPED`/`FAILED` row's protection stays
                # exactly as absolute as it always was. `auto_queue_suppressed` itself is never
                # touched by this branch -- see that function's own docstring for why the
                # eligibility flag and the state text are deliberately two separate questions.
                prev_state, _, _, _ = previous.get(rel_path, (None, None, None, None))
                corrected_state = (
                    local_delete.reconsider_removed_state(
                        prev_state,
                        remote_present=node.remote_size is not None,
                        local_present=node.local_size is not None,
                        structural_state=node.structural_state,
                    )
                    if prev_state is not None
                    else None
                )
                await self.db.execute(
                    """
                    INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, local_mtime, state)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                        is_dir = excluded.is_dir,
                        remote_size = excluded.remote_size,
                        local_size = excluded.local_size,
                        remote_mtime = excluded.remote_mtime,
                        local_mtime = excluded.local_mtime,
                        state = COALESCE(?, state)
                    """,
                    (
                        queue_id,
                        rel_path,
                        1 if node.is_dir else 0,
                        node.remote_size,
                        node.local_size,
                        node.remote_mtime,
                        node.local_mtime,
                        node.structural_state,
                        corrected_state,
                    ),
                )
                continue

            # DESIGN.md §3.2 rule 3, §7.3's grace period, required starting phase 4 (see
            # docs/decisions.md and core/mount_sentinel.py's module docstring): a fresh
            # REMOTE_ONLY reading for something that used to have a complete local copy (or
            # is already REMOVED_LOCAL) doesn't get written verbatim -- it's resolved against
            # history and the mount gate first. The branch above it is the same arbitration
            # for the opposite reading, presence rather than absence. Every other node's
            # freshly-computed state is trusted as-is, exactly like phase 2/3.
            prev_state, prev_substate, prev_first_missing_at, prev_remote_deleted_at = previous.get(
                rel_path, (None, None, None, None)
            )
            if postprocess.outcome_survives_rescan(
                prev_state, node.structural_state, remote_deleted_at=prev_remote_deleted_at
            ):
                # DESIGN.md §3.2/§6: the content is all still here, and a post-processing
                # outcome says something about it this scan's byte comparison cannot --
                # `core/postprocess.py` owns `state` for as long as that holds. Without this,
                # every outcome (including CORRUPT and EXTRACT_FAILED, the two a user most
                # needs to see) was silently overwritten with a plain DOWNLOADED by the next
                # scan, within 30 seconds of being set. `first_missing_at` is cleared for the
                # same reason the branch below clears it: local presence is not in question.
                state, first_missing_at = prev_state, None
            else:
                override = mount_sentinel.resolve_absence(
                    prev_state=prev_state,
                    prev_first_missing_at=prev_first_missing_at,
                    structural_state=node.structural_state,
                    mount_ok=mount_ok,
                    now=now,
                )
                state = override[0] if override is not None else node.structural_state
                first_missing_at = override[1] if override is not None else None

            # The settle gate's completion half (prompts/open-issues.md #2): a top-level item
            # that would otherwise publish DOWNLOADED is held at REMOTE_ONLY/substate=settling
            # until its remote fingerprint has held for `settle.REQUIRED_SETTLE_SCANS`
            # consecutive scans *and* `settle.SETTLE_MIN_AGE_S` of wall-clock time
            # (prompts/2026-08-12-settle-gate-followups.md item 2 -- the scan-count alone would
            # silently weaken once a queue's own scan interval can be shorter than today's one
            # global default). This is what actually fixes the directory case -- byte
            # comparison alone can't tell "3 of 8 files, all whole" from "done." Deliberately
            # simple rather than precise: an item that is genuinely, permanently complete but
            # merely hasn't had its second confirming scan yet is *also* shown REMOTE_ONLY for
            # one scan interval -- the flat 2-scan rule DESIGN.md open-issues #2 settled on
            # ("predictable beats clever"). Applied after the absence arbitration above, not
            # instead of it -- this only ever downgrades a *presence* reading.
            substate: str | None = None
            if (
                settle_settings.enabled
                and settle_record is not None
                and not settle.is_settled(settle_record, now=now.timestamp())
            ):
                if state == "DOWNLOADED":
                    state = "REMOTE_ONLY"
                    first_missing_at = None
                if state == "REMOTE_ONLY":
                    substate = "settling"

            # prompts/open-issues.md #2's stuck-item follow-up (this task): a job can finish
            # while its item is still unsettled (`core/queue.py._reap_one`'s completion gate),
            # which leaves it at REMOTE_ONLY/substate='settling' with its bytes already fully
            # on disk. Nothing re-queues it (auto-queue is off, or eligible again but coincides
            # with a busy queue, or nobody clicks) -- but this scan pass, right above, is what
            # actually knows the fingerprint just finished settling: `state` computed to
            # DOWNLOADED on its own, off the structural reading, with no fresh job involved.
            # Recognized here by the transition itself (previously *our own* gate's hold, now
            # DOWNLOADED) rather than by re-deriving "was this ever job-downloaded," which the
            # settle gate can't tell from a pre-existing local file anyway -- and doesn't need
            # to: both cases have complete bytes and no post-processing outcome yet, which is
            # exactly what makes triggering it here safe (see the trigger call below).
            if (
                prev_state == "REMOTE_ONLY"
                and prev_substate == "settling"
                and state == "DOWNLOADED"
            ):
                unstuck.add(rel_path)

            # `downloaded_at` backfill (prompts/open-issues.md "7 + 8": retention keys on this
            # column, not `state_changed_at`, and it is otherwise written in exactly one place
            # -- `core/queue.py`'s job-success path, `_reap_one`. An item that reaches
            # `DOWNLOADED` by reconcile alone (pre-existing local files on first scan, a
            # restart that resumes a scan-only view of a directory an external process
            # finished) never passes through that write and would sit at `downloaded_at IS
            # NULL` forever, silently invisible to retention. `COALESCE`-style: only ever fills
            # a NULL, so a rescan can never overwrite a real job's timestamp with "now" -- the
            # `COALESCE` reads the *stored* value on the `ON CONFLICT` branch (SQLite evaluates
            # the pre-update column, not `excluded`), and `downloaded_stamp` below is `None`
            # (so the `COALESCE` is a no-op) on every scan where this pass's own `state` isn't
            # `DOWNLOADED` -- computed from `state` after every arbitration above (the settle
            # gate included), so an item the settle gate is still holding at REMOTE_ONLY never
            # gets stamped as if it had completed.
            downloaded_stamp = (
                now.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if state == "DOWNLOADED" else None
            )

            await self.db.execute(
                """
                INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, local_mtime, state, substate, first_missing_at, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                    is_dir = excluded.is_dir,
                    remote_size = excluded.remote_size,
                    local_size = excluded.local_size,
                    remote_mtime = excluded.remote_mtime,
                    local_mtime = excluded.local_mtime,
                    state = excluded.state,
                    substate = excluded.substate,
                    first_missing_at = excluded.first_missing_at,
                    downloaded_at = COALESCE(downloaded_at, excluded.downloaded_at)
                """,
                (
                    queue_id,
                    rel_path,
                    1 if node.is_dir else 0,
                    node.remote_size,
                    node.local_size,
                    node.remote_mtime,
                    node.local_mtime,
                    state,
                    substate,
                    first_missing_at,
                    downloaded_stamp,
                ),
            )

        # 2026-08-13 (prompts/2026-08-13-move-mode-outcome-survives-local-only.md): a rel_path
        # that has left *both* trees entirely -- a `move`-mode item whose remote copy this
        # codebase already deleted and whose local copy `_do_move` then relocated to
        # `staging_path` (or an importer took, same shape) -- has no entry in `nodes` at all
        # (`core/reconcile.py`'s `all_paths` is `set(remote_tree) | set(local_tree)`, and this
        # path is in neither). The loop above only ever visits `nodes.values()`, so without
        # this, such a row is simply never written again: whatever outcome it last held
        # (`EXTRACTED`, say) would freeze forever instead of resolving through §7.3's grace
        # period the way an ordinary importer-moved-it-out item does -- defeating rule 3 for
        # exactly the items `move` mode produces the most of.
        #
        # Deliberately narrow, reusing `resolve_absence`'s own gate rather than re-implementing
        # it: `structural_state="REMOTE_ONLY"` is the closest existing reading for "there is
        # nothing here to compare" (the function only cares that local presence is in
        # question), and `resolve_absence` itself already returns `None` -- "trust the fresh
        # reading as-is" -- for any `prev_state` outside `_STICKY_PREV_STATES`. `None` here
        # means *skip*, not *trust*: there is no fresh structural reading for a path in neither
        # tree, so a `prev_state` this function has no opinion about (`LOCAL_ONLY`,
        # `REMOVED_BOTH`, a mid-flight `PARTIAL`/`QUEUED`) is left exactly as it was rather than
        # invented. `protected` is already excluded from `written`'s complement, so a
        # suppressed row (`STOPPED`/`FAILED`/a self-delete's `REMOVED_BOTH`) never reaches this
        # loop either -- consistent with rule 6's "never rescanned".
        vanished = set(previous) - written - protected
        for rel_path in sorted(vanished):
            prev_state, _prev_substate, prev_first_missing_at, _prev_remote_deleted_at = previous[
                rel_path
            ]
            if rel_path in deleted_archive_paths:
                # 2026-08-14 (prompts/2026-08-14-extracted-archives-rest-as-extracted.md): this
                # codebase deleted this file itself -- the successful conclusion of extraction
                # (`core/local_delete.py.delete_extracted_archives`), never a loss -- so §7.3's
                # grace clock (`resolve_absence` below) must not even start for it, on either
                # sync mode. A `copy` queue's remote volume survives cleanup, so this rel_path
                # never reaches this sweep at all: it is still in `all_paths` (the surviving
                # remote entry), and `reconcile()`'s own predicate check -- fed this exact set
                # via `build_scan_counts_predicate` -- already marks it `EXCLUDED` directly,
                # before `_persist` ever sees it. A `move` queue has *already* deleted the
                # remote copy too (`postprocess._maybe_delete_remote`, before extraction even
                # runs), so this rel_path is absent from *both* trees and lands here instead --
                # the live bug this closes: nine seconds after deletion, `resolve_absence` would
                # read the fresh "REMOTE_ONLY" as a disappearance and start a ten-minute
                # `first_missing_at` countdown for a file removed on purpose, eventually
                # resolving to `REMOVED_BOTH`. Resolving straight to `EXCLUDED` here instead
                # makes both sync modes rest identically (the open-issues entry this closes),
                # reusing the one existing state that already means "excluded from completeness
                # accounting, for a real reason" (DESIGN.md §3.2 rule 8) rather than adding a
                # new one -- `core/itemview.py.item_view`'s `deleted_archive_at` field is what
                # lets the frontend tell this `EXCLUDED` apart from an ordinary pattern-excluded
                # file and render it as a greyed-out "Extracted" chip, never "Excluded".
                vanished_state, vanished_first_missing_at = "EXCLUDED", None
            elif (
                override := mount_sentinel.resolve_absence(
                    prev_state=prev_state,
                    prev_first_missing_at=prev_first_missing_at,
                    structural_state="REMOTE_ONLY",
                    mount_ok=mount_ok,
                    now=now,
                )
            ) is not None:
                vanished_state, vanished_first_missing_at = override
                if vanished_state == "REMOVED_LOCAL":
                    # `prompts/open-issues.md` "resolve_absence never writes REMOVED_BOTH":
                    # `resolve_absence` itself is correct and untouched -- its real call site
                    # above (the ordinary per-node loop) means exactly "REMOTE_ONLY", remote
                    # genuinely present. *This* call site fakes that reading ("the closest
                    # existing reading for 'there is nothing here to compare'", per the comment
                    # above) for a `rel_path` this pass already knows is in neither tree, so its
                    # only two truthful terminal states are the vanished-sweep's own coinage,
                    # `REMOVED_BOTH` -- correct whether the grace clock just expired or was
                    # already sitting sticky at `REMOVED_LOCAL` from a *previous* pass, since a
                    # `REMOVED_LOCAL` row only ever gets here by losing its remote copy (its
                    # last known-good structural reading) between that pass and this one. Left
                    # unsuppressed, deliberately, same as `resolve_vanished`'s own `REMOVED_BOTH`
                    # a few lines down: nothing here asserts *who* removed the remote copy, and
                    # `REMOVED_BOTH` is already excluded from `core/autoqueue.py.ELIGIBLE_STATES`
                    # by name (not by `auto_queue_suppressed`), so no flag is needed to keep this
                    # row out of auto-queue -- closing the 🔴 issue right above this one in
                    # open-issues.md: a bare `REMOVED_LOCAL` here is what let
                    # `re_download_externally_removed` queue a doomed job against a remote that
                    # no longer exists. See docs/decisions.md for the full reasoning.
                    vanished_state = "REMOVED_BOTH"
            else:
                # 2026-08-13 (prompts/2026-08-13-delete-state-truthfulness.md, defect 3):
                # `resolve_absence` has no opinion about this `prev_state` (PARTIAL, LOCAL_ONLY,
                # EXCLUDED, or REMOVED_BOTH already resting here) -- without a fallback such a
                # row is simply never written again, frozen forever on a reading nothing will
                # ever revisit (see `resolve_vanished`'s own docstring for the bug this closes).
                fallback_state = mount_sentinel.resolve_vanished(prev_state)
                if fallback_state is None:
                    continue
                vanished_state, vanished_first_missing_at = fallback_state, None

            # 2026-08-13 (prompts/2026-08-13-vanished-rows-should-leave-the-tree.md): `written`
            # is not just "persist this" any more -- `_project` filters publication by it -- so
            # the row this loop just resolved to a *terminal* removed state (kept "as history"
            # by design, `_project`'s own docstring) must NOT re-enter `written`. It still gets
            # written below, every pass, so the History page (which reads `item` directly, not
            # through `written`/`_project`) keeps seeing it. Only a *non-terminal* resolution --
            # still holding a content-asserting outcome state during the grace period, or (for
            # the `resolve_vanished` fallback) there is no non-terminal outcome it can reach --
            # re-enters `written` and keeps showing up in the Files tree, exactly per the rule:
            # publish while the grace period runs, stop once it lands on `REMOVED_LOCAL`/
            # `REMOVED_BOTH` with nothing left in either tree.
            if vanished_state not in _TERMINAL_REMOVED_STATES:
                written.add(rel_path)
            await self.db.execute(
                "UPDATE item SET remote_size = NULL, local_size = NULL, local_mtime = NULL, "
                "state = ?, first_missing_at = ? WHERE queue_id = ? AND rel_path = ?",
                (vanished_state, vanished_first_missing_at, queue_id, rel_path),
            )

        if new_settle:
            await settle.save_settle_records(self.db, queue_id, new_settle)
        await self.db.commit()

        # prompts/open-issues.md #2's stuck-item follow-up: for every rel_path this pass just
        # released from the settle gate straight to DOWNLOADED (`unstuck`, built above), fire
        # post-processing exactly the way `core/queue.py._reap_one` does on a job's own success
        # -- this **is** the item's first (and only) transition into DOWNLOADED with nothing
        # left pending, `core/postprocess.py`'s own precondition for the trigger. This widens
        # that module's documented trigger contract from "job success only" to "job success, or
        # a scan releasing this exact gate's own hold" -- DESIGN.md §6 currently says only the
        # former and needs a follow-up correction (see docs/decisions.md, this task's entry, and
        # `prompts/open-issues.md` #2). Not a general scan-driven trigger: `unstuck` is narrowly
        # the settle gate's own prior hold resolving, never a plain first-scan-finds-existing-
        # files DOWNLOADED (which stays out of scope, unchanged, exactly as before this task).
        # Safe against a double-fire because `prev_state`/`prev_substate` are read fresh every
        # pass: once this write lands, the *next* scan's `prev_state` is `DOWNLOADED`, not
        # `REMOTE_ONLY`/`settling`, so the same item can't retrigger on the following pass --
        # and `_process_item` itself re-checks `item.state == 'DOWNLOADED'` before doing
        # anything, so even a theoretical duplicate trigger is a safe no-op, never a rerun of an
        # outcome already recorded.
        if unstuck and self.postprocess is not None:
            placeholders = ",".join("?" for _ in unstuck)
            cursor = await self.db.execute(
                f"SELECT id FROM item WHERE queue_id = ? AND rel_path IN ({placeholders})",  # noqa: S608 - placeholders only, no interpolated values
                (queue_id, *sorted(unstuck)),
            )
            for row in await cursor.fetchall():
                self.postprocess.trigger(row["id"])

        return written

    async def _project(self, queue_id: int, rel_paths: set[str]) -> dict[str, ItemView]:
        """Read one queue's persisted rows back as the projection everything publishes
        (`core/itemview.py`), keyed by `rel_path`.

        This is the query `_refresh_item_ids` used to run — `SELECT ... FROM item WHERE
        queue_id = ?`, once per scan, after the upsert so freshly-inserted rows are included —
        widened from `id, rel_path` to the display columns. No new query, no extra round trip,
        and no asymptotic change: `reconcile` is already O(tree).

        **`rel_paths` filters the result to what this pass is entitled to publish**, and it is
        not incidental. Nothing ever deletes an `item` row (§3.2 rule 6 keeps `REMOVED_BOTH`
        as history, and every other vanished path simply stops being upserted), so an
        unfiltered projection would resurrect rows that left both trees — and would leave
        `diff_nodes`'s `removed` list permanently empty, since a row that is never deleted can
        never disappear from the projection. Passing the set `_persist` just wrote keeps the
        published node set identical to the reconciled one, exactly as before; only the
        *values* now come from the database.

        **`LEFT JOIN item_settle`** (2026-08-13, `prompts/2026-08-13-files-ux-pass.md` item 3):
        the Files page's settle-gate countdown needs `item_settle.matched_scans`/`updated_at`
        for top-level rows, and this is the one place both `queue_delta` (`scan_queue`, below)
        and connect-time `snapshot()` read the `item` table back from. `item_settle`'s own
        primary key is `(queue_id, rel_path)`, so `EXPLAIN QUERY PLAN` confirms this is a
        per-row indexed lookup (`SEARCH settle USING INDEX sqlite_autoindex_item_settle_1
        (queue_id=? AND rel_path=?) LEFT-JOIN`), never a second table scan. Measured directly
        (`sqlite3`, in-memory, warmed) against a synthetic 20,800-row tree (800 top-level items,
        25 files each, every top-level item carrying an `item_settle` row -- the real worst
        case, since `_persist` advances that row for every top-level item on every scan
        regardless of `state`): ~20.0ms/query unjoined vs. ~23.4ms/query joined, +3.4ms per
        call. Called once per scan (`scan_interval_s`, default 30s) and once per new WebSocket
        connection (`snapshot()`) -- +3.4ms at either cadence is not worth avoiding at any queue
        size this project targets. docs/decisions.md records the method and numbers.

        Three more columns joined from the same `settle` alias (2026-08-13,
        `prompts/2026-08-13-settle-progress-visibility.md`, migration 013) -- `total_bytes`,
        `first_observed_at`, `last_changed_at` -- for the "still arriving" display
        (`core/itemview.py.item_view`). Same single indexed per-row lookup, no second join, no
        re-measurement warranted for three more `INTEGER`/`TEXT` columns off a row already being
        fetched.

        **`LEFT JOIN deleted_archive`** (2026-08-14,
        `prompts/2026-08-14-extracted-archives-rest-as-extracted.md`): the same per-row indexed
        lookup as `item_settle` above (`deleted_archive`'s own primary key is also
        `(queue_id, rel_path)`), giving `core/itemview.py.item_view` `deleted_archive_at` so the
        Files page can tell a spent, on-purpose-removed archive volume (`EXCLUDED` for this
        reason) apart from an ordinary pattern-`EXCLUDED` file and render it as a greyed-out
        "Extracted" chip.
        """
        cursor = await self.db.execute(
            f"SELECT {ITEM_VIEW_COLUMNS_QUALIFIED}, "  # noqa: S608 - a module constant, not user input
            "settle.matched_scans AS settle_matched_scans, "
            "settle.updated_at AS settle_first_matched_at, "
            "settle.total_bytes AS settle_total_bytes, "
            "settle.first_observed_at AS settle_first_observed_at, "
            "settle.last_changed_at AS settle_last_changed_at, "
            "deleted_archive.deleted_at AS deleted_archive_at "
            "FROM item "
            "LEFT JOIN item_settle AS settle "
            "ON settle.queue_id = item.queue_id AND settle.rel_path = item.rel_path "
            "LEFT JOIN deleted_archive "
            "ON deleted_archive.queue_id = item.queue_id AND deleted_archive.rel_path = item.rel_path "
            "WHERE item.queue_id = ?",
            (queue_id,),
        )
        return {
            row["rel_path"]: item_view(row)
            for row in await cursor.fetchall()
            if row["rel_path"] in rel_paths
        }

    async def snapshot(self) -> list[dict[str, Any]]:
        """The full current model, one message per queue — what a freshly-connected
        WebSocket client gets before any delta (DESIGN.md §2, §9). This is the *only* place
        a full node list is ever sent; every update after connect is a `queue_delta`
        (`scan_queue`) or an `item_delta` (`core/queue.py`), each proportional to what
        changed rather than to the size of the tree.

        **Re-reads the database rather than serving `self.models` verbatim, and is `async`
        for that reason.** The cached model is only refreshed on a scan (up to
        `scan_interval_s`, default 30s), while `core/queue.py` and `core/postprocess.py` write
        `item.state` the instant a job or a pipeline step moves — they push an `item_delta`
        for it, but a client connecting *after* that push and *before* the next scan would
        otherwise be handed a snapshot older than the database it is meant to reflect. Reload
        is precisely how the structural-vs-persisted disagreement used to become visible
        (`core/itemview.py`), so the connect path reads back like every other publisher. The
        cost is one query per queue per connection, not per scan.
        """
        out = []
        for queue_id, nodes in self.models.items():
            meta = self.queue_meta.get(queue_id)
            current = await self._project(queue_id, set(nodes))
            out.append(
                {
                    "type": "queue_snapshot",
                    "queue_id": queue_id,
                    "queue_name": meta.name if meta else "",
                    "nodes": list(current.values()),
                    "scanned_at": self.last_scan_at.get(queue_id),
                    "warning": self.scan_warnings.get(queue_id),
                }
            )
        return out
