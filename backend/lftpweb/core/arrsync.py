"""The *arr sync poller (docs/arr-integration-spec.md "The poller") -- background loop, same
`_task`/`start()`/`stop()` shape as `core/backup.py.BackupScheduler`, matching a bound Sonarr/
Radarr instance's `/api/v3/queue` against local items, and watching for import (or removal).

**Not wired into the scan pass** -- scan cadence is per-queue and variable (DESIGN.md §5), and
*arr polling wants its own clock, independent of it (spec: "not wired into the scan pass").

**Phase A** (`prompts/done/2026-08-15-arr-integration-backend.md`) built matching
(`(no status) -> detected`) and import/removal detection (`detected/notified -> imported/gone`).
**Phase B** (`prompts/2026-08-15-arr-integration-notify-cleanup.md`, this module now) adds the
two active behaviors on top: a **bounded retry** for a notify push whose primary attempt (fired
from `core/postprocess.py.PostprocessPipeline`'s own tail, per the spec's "Notify" section) has
already failed once (`_maybe_retry_notify` -- the actual POST and event-writing live in
`core/arrnotify.py.notify_arr`, shared by both callers so there is exactly one implementation),
and **cleanup** (`_maybe_cleanup`) for an `imported` item on an `arr_delete_completed` queue.

**Rung 4 of the move-mode delete ladder** (`_maybe_delete_remote_on_import`, added 2026-08-16,
`prompts/done/2026-08-16-move-delete-gate-ladder.md`, resolving open issue #2 /
`docs/audit-v0.1.0.md` G1): `core/postprocess.py._maybe_delete_remote` defers a `move`-mode
item's remote delete here, rather than performing it, whenever the item is *arr-tracked
(`arr_status` non-null) by the time its own pipeline run reaches the delete gate -- recorded in
`item.remote_delete_pending`. This module performs the deferred delete (via
`perform_remote_delete`, the one implementation, never a second one) the moment `_commit_terminal`
confirms `imported`, and *before* this poller pass's own `arr_delete_completed` cleanup sweep --
so "import green -> delete source -> (optionally) delete local" holds even within a single pass.
Never on `gone`. `remote_pool`/`host_provider` are optional, plain-attribute-after-construction
seams (like `in_flight_provider`/`delete_in_flight` below) -- `None` (a test fixture that doesn't
wire them) simply leaves a deferred item deferred for a later pass, the same "no-op until wired"
shape `_maybe_notify_arr` uses for a missing `config_dir`.

**The two-consecutive-passes quiescence guard is in-memory, not persisted** (deliberately: the
spec's "Data model" section specifies exactly three new `item` columns and no new table for this
feature, unlike `core/settle.py`'s `item_settle`). A restart loses any pending candidacy and
simply costs one extra poll interval before a transition commits -- safe, since "wait longer
before the irreversible step" is the direction restart-loss is allowed to err in. The notify
retry's own bounded-attempt counter (`_notify_attempts`) is in-memory for the identical reason;
losing it on restart only means a slow-to-notify item gets a few more attempts than the bound
technically allows, never fewer. See `_PendingVerdict` below.

**Cleanup deliberately never writes `item.state` directly.** Unlike a manual Files-page delete
(`core/local_delete.py.delete_local`, which sets `REMOVED_LOCAL`/`REMOVED_BOTH` immediately,
because a human just confirmed the action), `_maybe_cleanup` removes the bytes and leaves
`item.state` exactly as it was -- the same pattern `core/postprocess.py._do_move` already
established for a staging relocation ("the next scan finds the item's local copy gone...and
[mount_sentinel's] REMOVED_LOCAL grace-period machinery takes it from there, the same as any
other externally-caused move"). This is what makes the spec's own UX description literally true
("downloaded -> processed -> (countdown) -> gone") rather than aspirational: the existing
`first_missing_at`/`REMOVAL_GRACE_ELIGIBLE_STATES` countdown chip
(`frontend/src/lib/format.ts`) only ever renders while a row is *not yet* `REMOVED_LOCAL` --
`delete_local`'s own immediate write would skip straight past that window and the chip would
never appear. See `docs/decisions.md` (2026-08-15) for the full reasoning and why this reads
"the existing local-deletion machinery" narrowly (its resolvers and guards, not its
state-writing tail).
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from lftpweb.core import audit, extract, mount_sentinel
from lftpweb.core.arrclient import (
    TRACKED_DOWNLOAD_STATE_IMPORTED,
    ArrClient,
    ArrClientError,
    HistoryEvent,
    QueueRecord,
)
from lftpweb.core.arrnotify import notify_arr
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view
from lftpweb.core.local_delete import DeleteInFlight, _do_remove_from_disk, _physical_local_root
from lftpweb.core.postprocess import perform_remote_delete

logger = logging.getLogger(__name__)

# --- Settings (JSON in `setting`, same pattern as `core/backup.py.BackupSettings`) ---------

SETTING_KEY = "arr_settings"


@dataclass(frozen=True)
class ArrSettings:
    """Site-level poll cadence (docs/arr-integration-spec.md "The poller": "Default every 60s
    (site setting, `ArrSettings` in `setting` like every other settings dataclass)"). Not itself
    an on/off switch -- an instance's own `enabled` column is that (migration 018, "everything
    defaults OFF"); this only governs how often an *enabled* instance's queue is polled.
    """

    poll_interval_s: float = 60.0


async def load_arr_settings(db: aiosqlite.Connection) -> ArrSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return ArrSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return ArrSettings()
    return ArrSettings(poll_interval_s=float(data.get("poll_interval_s", 60.0)))


async def save_arr_settings(db: aiosqlite.Connection, settings: ArrSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES "
        "(?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps({"poll_interval_s": settings.poll_interval_s})),
    )
    await db.commit()


# --- Matching (docs/arr-integration-spec.md "Matching") -------------------------------------

_NORMALIZE_RE = re.compile(r"[._ ]+")


def _normalize_name(name: str) -> str:
    """Case-fold, `.`/`_`/space equivalence (spec: "title normalized (case-fold, `.`/`_`/space
    equivalence)") -- `"Show.S01E05.1080p-GRP"` and `"Show S01E05 1080p-GRP"` normalize to the
    same string.
    """
    return _NORMALIZE_RE.sub(" ", name.casefold()).strip()


def _record_matches_item(record: QueueRecord, item_name: str) -> bool:
    """Match, in the spec's own order: basename of `outputPath` first (exact, the normal case),
    then normalized `title` (covers single-file releases and renaming clients). `item_name` is
    the item's **logical** top-level name (`item.rel_path` for a top-level row is already that
    -- `core/local_scan.py` maps `.downloading-<name>` back to `<name>` before it ever reaches
    the `item` table, so no physical-path handling belongs here; see the five-defects lesson the
    spec itself cites).
    """
    if record.output_path:
        basename = posixpath.basename(record.output_path.rstrip("/"))
        if basename and basename == item_name:
            return True
    if not record.title:
        return False
    return _normalize_name(record.title) == _normalize_name(item_name)


# Association states a fresh queue record is allowed to match against: never-associated, or a
# terminal one a regrab can restart (spec "Failure modes": "a second record matching an
# already-`cleaned` item name must start a *fresh* association, not resurrect the old one").
# `imported`/`detected`/`notified` are deliberately excluded -- an actively-tracked association
# is never re-matched out from under itself.
_REMATCHABLE_STATES = frozenset({"gone", "cleaned"})

# States considered "still being watched for import" (spec "The poller" step 3).
_TRACKED_STATES = frozenset({"detected", "notified"})

# `item.state` values a notify-retry attempt is allowed to fire against (spec "Notify":
# fires "after the whole pipeline succeeds"). An item can be matched (`arr_status == 'detected'`)
# well before its own download even finishes -- the *arr's queue is populated by its own
# download client on the seedbox, independently of lftpweb's transfer -- so the retry must not
# push a scan command for a release that is still `REMOTE_ONLY`/`PARTIAL`/`DOWNLOADING`/mid
# transient-postprocess-state; only these three terminal, successful outcomes count.
_NOTIFY_READY_STATES = frozenset({"DOWNLOADED", "VERIFIED", "EXTRACTED"})

# Bounded retry cap (spec "Notify": "bounded retries") -- an instance that is simply down for a
# long stretch gets this many attempts, roughly this many poll intervals apart, before this
# module stops trying; CDH may still import the release on its own regardless.
MAX_NOTIFY_RETRY_ATTEMPTS = 5


def _now_iso() -> str:
    """Same wall-clock format `core/audit.py.record_event` stamps `event.ts` with -- one
    convention for "a Python-side UTC timestamp," not a second one invented here.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Two-pass quiescence guard (spec "The association lifecycle": "Both signals must hold on
# two consecutive poller passes") -------------------------------------------------------------


@dataclass(frozen=True)
class _PendingVerdict:
    """One item's not-yet-confirmed candidacy for `imported` or `gone`, from the *previous*
    poll pass. `download_id` is carried alongside the verdict so a candidacy computed against
    one association never confirms a *different* one that happens to reach the same item id
    between passes (the regrab case, spec "Failure modes": "Keyed on (item id, downloadId) when
    deciding 'new match'" -- the same discipline applied here to the *confirmation* step, not
    just the match step).
    """

    verdict: Literal["imported", "gone"]
    download_id: str | None


# --- Per-instance failure isolation (spec "The poller": "capped exponential backoff") -------

INITIAL_BACKOFF_S = 60.0
MAX_BACKOFF_S = 1800.0  # 30 minutes
BACKOFF_FACTOR = 2.0


@dataclass
class _InstanceBackoff:
    delay_s: float
    next_attempt_at: float  # time.monotonic()


# --- The poller itself ------------------------------------------------------------------------


class ArrSyncScheduler:
    """Background loop, same `_task`/`start()`/`stop()` shape as `core/backup.py.
    BackupScheduler` and `core/local_delete.py`'s (via `core/retention.py`) `RetentionScheduler`
    -- one bad cycle must not kill the loop, `stop()` cancels cleanly on shutdown.

    `config_dir` is needed to decrypt each instance's `api_key_enc` (`core/crypto.py`, same
    convention as the seedbox password) fresh on every poll pass -- an `ArrClient` is
    constructed per instance per pass and closed again immediately after, so a plaintext key
    never outlives the pass that used it. `events` is the same plain-attribute-after-
    construction seam `RetentionScheduler.events` uses; `None` (this module's own tests that
    don't care about the WS side) simply means no `item_delta` is published.

    Phase B (docs/arr-integration-spec.md "Cleanup") adds `in_flight_provider`/
    `delete_in_flight`, the identical seam `core/local_delete.py.RetentionScheduler` takes:
    cleanup's own filesystem removal must be shielded from (and must itself shield) a scan
    racing it, the same "in-memory, protected only while a worker actually holds it" guarantee
    every other deleter in this codebase gets. Both default to `None` (no-op) so every existing
    test of this module that never touches cleanup is unaffected.

    Rung 4 of the move-mode delete ladder (this module's own docstring, 2026-08-16) adds
    `remote_pool`/`host_provider` -- the identical seam `core/postprocess.py.PostprocessPipeline`
    takes for the same job (`RemoteConnectionPool.delete_path`, and the callable that decrypts
    the seedbox host config), loosely typed (`Any`) for the same reason that constructor leaves
    `host_provider` loose. Both default to `None` so every existing test of this module is
    unaffected; production wiring (`main.py`) passes `app.state.engine.pool` and the same
    `_host_provider` closure `PostprocessPipeline` gets.
    """

    MIN_POLL_INTERVAL_S = 5.0  # floor against a misconfigured near-zero setting

    def __init__(
        self,
        db: aiosqlite.Connection,
        config_dir: str,
        events: EventBus | None = None,
        in_flight_provider: Callable[[], frozenset[int]] | None = None,
        delete_in_flight: DeleteInFlight | None = None,
        remote_pool: Any = None,
        host_provider: Any = None,
    ) -> None:
        self.db = db
        self.config_dir = config_dir
        self.events = events
        self.in_flight_provider = in_flight_provider
        self.delete_in_flight = delete_in_flight
        self.remote_pool = remote_pool
        self.host_provider = host_provider
        self._task: asyncio.Task | None = None
        self._backoff: dict[int, _InstanceBackoff] = {}
        self._pending: dict[int, _PendingVerdict] = {}
        # Bounded notify-retry attempts, keyed by item id -- in-memory, same "restart loses
        # pending state, and that is the safe direction" reasoning as `_pending` above (spec:
        # "Notify failure is non-fatal ... bounded retries on subsequent poller ticks").
        self._notify_attempts: dict[int, int] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-arr-sync-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("*arr sync cycle failed")
            settings = await load_arr_settings(self.db)
            await asyncio.sleep(max(settings.poll_interval_s, self.MIN_POLL_INTERVAL_S))

    # --- One pass over every enabled instance ------------------------------------------------

    async def run_once(self) -> None:
        cursor = await self.db.execute(
            "SELECT id, name, kind, base_url, api_key_enc FROM arr_instance WHERE enabled = 1"
        )
        instances = await cursor.fetchall()
        for instance in instances:
            await self._process_instance(instance)

    async def _process_instance(self, instance: aiosqlite.Row) -> None:
        instance_id = instance["id"]

        backoff = self._backoff.get(instance_id)
        if backoff is not None and time.monotonic() < backoff.next_attempt_at:
            return  # still backing off; never blocks other instances (spec)

        # `SELECT *` -- phase B's notify retry and cleanup need `local_path`/`staging_path`/
        # `arr_visible_path`/`name` alongside `id`/`arr_delete_completed`, and a queue row is
        # cheap; simpler than growing this column list every time a later phase needs one more.
        cursor = await self.db.execute(
            "SELECT * FROM path_queue WHERE arr_instance_id = ? AND enabled = 1",
            (instance_id,),
        )
        queues = await cursor.fetchall()
        if not queues:
            return  # spec: "For each enabled instance with >=1 bound queue"

        try:
            api_key = decrypt_secret(self.config_dir, instance["api_key_enc"])
        except DecryptionError as exc:
            await self._handle_failure(instance_id, instance["name"], exc)
            return

        async with ArrClient(
            kind=instance["kind"], base_url=instance["base_url"], api_key=api_key
        ) as client:
            try:
                records = await client.queue_records()
            except ArrClientError as exc:
                await self._handle_failure(instance_id, instance["name"], exc)
                return

            self._backoff.pop(instance_id, None)  # reachable again

            for queue in queues:
                try:
                    await self._process_queue(client, queue, records)
                except ArrClientError as exc:
                    # A history lookup mid-pass failed -- the instance just went unreachable
                    # partway through; whatever already committed for earlier queues this pass
                    # stands (each write is its own transaction), the rest waits for the next
                    # attempt after backoff.
                    await self._handle_failure(instance_id, instance["name"], exc)
                    return

    async def _handle_failure(self, instance_id: int, instance_name: str, exc: Exception) -> None:
        """One WARNING, one event row, then back off -- never blocks or slows the loop for
        other instances (spec). `exc` may be an `ArrClientError` (unreachable/non-2xx) or a
        `DecryptionError` (the stored API key can no longer be decrypted, e.g. a rotated
        install secret) -- both mean the same thing to the poller: this instance cannot be
        used right now.
        """
        prior = self._backoff.get(instance_id)
        delay = (
            INITIAL_BACKOFF_S
            if prior is None
            else min(prior.delay_s * BACKOFF_FACTOR, MAX_BACKOFF_S)
        )
        self._backoff[instance_id] = _InstanceBackoff(
            delay_s=delay, next_attempt_at=time.monotonic() + delay
        )
        logger.warning(
            "*arr instance %d (%s) unreachable, backing off %.0fs: %s",
            instance_id,
            instance_name,
            delay,
            exc,
        )
        await audit.record_event(
            self.db,
            level="warning",
            kind="arr_unreachable",
            message=(
                f"*arr instance {instance_name!r} (id={instance_id}) unreachable: {exc}; "
                f"backing off {delay:.0f}s"
            ),
        )

    # --- One bound queue, given the instance's already-fetched queue records -----------------

    async def _process_queue(
        self, client: ArrClient, queue: aiosqlite.Row, records: list[QueueRecord]
    ) -> None:
        queue_id = queue["id"]
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_status, arr_download_id, state, pending_download_prefix "
            "FROM item WHERE queue_id = ? AND instr(rel_path, '/') = 0",
            (queue_id,),
        )
        items = await cursor.fetchall()

        await self._match_items(queue_id, items, records)

        tracked = [i for i in items if i["arr_status"] in _TRACKED_STATES]
        for item in tracked:
            if item["arr_status"] == "detected":
                # Phase B (spec "Notify"): the *primary* push already happened, or didn't
                # happen at all yet, from `PostprocessPipeline`'s own tail -- this is only the
                # bounded retry for a primary attempt that failed (`notify_arr`'s own
                # `item.arr_status == 'detected'` gate is what actually decides "notified"
                # never re-enters this branch, not this `if`).
                await self._maybe_retry_notify(queue, item)
            await self._check_import(client, queue, item, records)

        if queue["arr_delete_completed"]:
            # A fresh query, not the stale `items` snapshot above -- `_check_import` may have
            # just committed an item to `imported` this very pass (spec: "Withheld is
            # re-evaluated on later passes, not terminal" implies the reverse too: a
            # newly-imported item must not wait an extra pass before cleanup is even
            # considered). It also runs *after* `_check_import`'s own rung-4 remote delete
            # (`_commit_terminal` -> `_maybe_delete_remote_on_import`), so an imported item this
            # very pass already has its remote copy gone before this sweep even queries --
            # "import green -> delete source -> (optionally) delete local," per the ladder.
            cursor = await self.db.execute(
                "SELECT * FROM item WHERE queue_id = ? AND arr_status = 'imported'", (queue_id,)
            )
            imported_items = await cursor.fetchall()
            for item in imported_items:
                await self._maybe_cleanup(queue, item)

    # --- Notify retry (spec "Notify": "retry on the next poller tick (bounded retries)") -----

    async def _maybe_retry_notify(self, queue: aiosqlite.Row, item: aiosqlite.Row) -> None:
        """Retry a notify push whose primary attempt (`PostprocessPipeline`'s own tail) already
        failed -- or push for the first time, if the primary attempt never got the chance to
        (e.g. this instance's `notify_on_complete` was turned on after the item's pipeline run
        already finished). Gated on the item having reached a stable, successful local outcome
        (`_NOTIFY_READY_STATES`, no pending download-prefix rename, no active job) -- the *arr's
        own queue can (and normally does) list a release before lftpweb has even finished
        pulling it down, so `arr_status == 'detected'` alone is not evidence the pipeline is
        done.
        """
        item_id = item["id"]
        if item["state"] not in _NOTIFY_READY_STATES or item["pending_download_prefix"] is not None:
            return
        if self._notify_attempts.get(item_id, 0) >= MAX_NOTIFY_RETRY_ATTEMPTS:
            return

        cursor = await self.db.execute(
            "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
            (item_id,),
        )
        if await cursor.fetchone() is not None:
            return  # not stable yet -- don't push mid-job

        final_root = await self._resolve_final_physical_root(queue, item["rel_path"])
        if final_root is None:
            return  # bytes not found at either known location this pass -- try again later

        outcome = await notify_arr(
            self.db,
            config_dir=self.config_dir,
            item=item,
            queue=queue,
            final_local_root=final_root,
            events=self.events,
        )
        if outcome == "failed":
            self._notify_attempts[item_id] = self._notify_attempts.get(item_id, 0) + 1
        else:
            self._notify_attempts.pop(item_id, None)

    # --- Shared path resolution: notify retry and cleanup both need "where are this item's
    # bytes right now" ---------------------------------------------------------------------

    async def _resolve_final_physical_root(
        self, queue: aiosqlite.Row, rel_path: str
    ) -> Path | None:
        """Where this item's bytes actually are, for a push or a delete that must act on the
        real location. Always asks `core/local_delete.py._physical_local_root` first -- the one
        resolver for the download-prefix-in-flight case, never a second one (per this project's
        own five-defects lesson). That resolver only ever accounts for the download-prefix
        namespace, never a queue's own `auto_move` relocation to `staging_path`
        (`core/postprocess.py._do_move`) -- so when its answer doesn't exist on disk, the one
        other place a finished item's bytes can legitimately be is `staging_path/rel_path`,
        checked here as a narrow, named fallback specific to this concern, not a general-purpose
        second resolver for the concern `_physical_local_root` already owns.

        `None` when neither location has anything on disk -- a legitimate outcome (bytes not
        there yet, or already gone), not an error; every caller treats it as "nothing to do this
        pass."
        """
        root = Path(queue["local_path"].rstrip("/"))
        candidate = await _physical_local_root(
            self.db, queue_id=queue["id"], root=root, rel_path=rel_path
        )
        if candidate.exists() or candidate.is_symlink():
            return candidate
        staging = queue["staging_path"]
        if staging:
            candidate2 = Path(staging.rstrip("/")) / rel_path
            if candidate2.exists() or candidate2.is_symlink():
                return candidate2
        return None

    # --- Cleanup (spec "Cleanup") -------------------------------------------------------------

    async def _maybe_cleanup(self, queue: aiosqlite.Row, item: aiosqlite.Row) -> None:
        """For an item whose association reached `imported` on an `arr_delete_completed` queue:
        withhold (named reason, re-evaluated next pass -- never terminal) when verification for
        this item failed or a job is active for it; otherwise suppress re-download, then remove
        the local bytes, then record `cleaned`.

        **Deliberately never writes `item.state`.** See this module's own docstring for why:
        the bytes disappearing is left for the ordinary scan + `core/mount_sentinel.py`
        absence-grace machinery to discover and carry to `REMOVED_LOCAL` on its own clock,
        exactly as if an external mover (a human, or the *arr's own hardlink pickup) had taken
        them -- "no new timer," per the spec.
        """
        item_id = item["id"]
        queue_id = queue["id"]

        if item["state"] == "CORRUPT":
            await self._record_cleanup_withheld(
                item_id, queue, "verification for this item failed (state=CORRUPT)"
            )
            return

        cursor = await self.db.execute(
            "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
            (item_id,),
        )
        if await cursor.fetchone() is not None:
            await self._record_cleanup_withheld(
                item_id, queue, "an active job exists for this item"
            )
            return

        # Suppress FIRST, before anything on disk is touched (spec "Cleanup" step 1) --
        # belt-and-braces against a copy-mode queue's auto-queue re-grabbing the still-present
        # remote copy while cleanup is in flight.
        await self.db.execute("UPDATE item SET auto_queue_suppressed = 1 WHERE id = ?", (item_id,))
        await self.db.commit()

        local_root = await self._resolve_final_physical_root(queue, item["rel_path"])
        if local_root is not None:
            local_path_root = Path(queue["local_path"].rstrip("/"))
            resolved = extract.resolve_within_root(local_root, local_path_root)
            if resolved is None and queue["staging_path"]:
                # The candidate may legitimately have come from `staging_path` instead (an
                # `auto_move` queue) -- re-check containment against *that* root before giving
                # up, rather than declaring an escape against a root the candidate was never
                # claiming to be under.
                resolved = extract.resolve_within_root(
                    local_root, Path(queue["staging_path"].rstrip("/"))
                )
            if resolved is None:
                await self._record_cleanup_withheld(
                    item_id,
                    queue,
                    f"{local_root} resolves outside the queue's known roots -- refusing",
                )
                return
            if not mount_sentinel.check(queue["local_path"].rstrip("/")):
                await self._record_cleanup_withheld(
                    item_id,
                    queue,
                    "local root is missing, unreadable, or has not completed a mount-sentinel scan",
                )
                return

            in_flight = self.in_flight_provider() if self.in_flight_provider else frozenset()
            if item_id in in_flight:
                await self._record_cleanup_withheld(
                    item_id, queue, "a post-processing worker is currently running for this item"
                )
                return
            if (
                self.delete_in_flight is not None
                and item_id in self.delete_in_flight.in_flight_item_ids()
            ):
                await self._record_cleanup_withheld(
                    item_id, queue, "a delete is already in progress for this item"
                )
                return

            if self.delete_in_flight is not None:
                self.delete_in_flight.mark([item_id])
            try:
                await asyncio.to_thread(_do_remove_from_disk, local_root, resolved)
            except OSError as exc:
                await self._record_cleanup_withheld(item_id, queue, f"local delete failed: {exc}")
                return
            finally:
                if self.delete_in_flight is not None:
                    self.delete_in_flight.unmark([item_id])
        # else: nothing found at either known location -- the goal state (no local copy) already
        # holds, so this proceeds to record `cleaned` rather than withholding forever on an item
        # that's already gone.

        await self.db.execute(
            "UPDATE item SET arr_status = 'cleaned', arr_status_at = ? WHERE id = ?",
            (_now_iso(), item_id),
        )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info",
            item_id=item_id,
            kind="arr_cleanup",
            message=(
                f"queue {queue_id} ({queue['name']!r}): local copy removed after confirmed *arr "
                "import -- item.state left as-is for the normal absence-grace machinery to carry "
                "it to REMOVED_LOCAL, same as any externally-caused removal"
            ),
        )
        await self._publish_item(queue_id, item_id)

    async def _record_cleanup_withheld(
        self, item_id: int, queue: aiosqlite.Row, reason: str
    ) -> None:
        await audit.record_event(
            self.db,
            level="warning",
            item_id=item_id,
            kind="arr_cleanup_withheld",
            message=f"queue {queue['id']} ({queue['name']!r}): cleanup withheld -- {reason}",
        )

    # --- Matching: (no status) | gone | cleaned -> detected ----------------------------------

    async def _match_items(
        self, queue_id: int, items: list[aiosqlite.Row], records: list[QueueRecord]
    ) -> None:
        candidates = [
            i for i in items if i["arr_status"] is None or i["arr_status"] in _REMATCHABLE_STATES
        ]
        if not candidates:
            return

        used_record_ids: set[int] = set()
        for item in candidates:
            matched: QueueRecord | None = None
            for record in records:
                if id(record) in used_record_ids:
                    continue
                if not _record_matches_item(record, item["rel_path"]):
                    continue
                # A terminal association only restarts on a genuinely *different* downloadId
                # (spec: "a second record matching an already-`cleaned` item name must start a
                # fresh association, not resurrect the old one" -- the identical downloadId
                # reappearing is not a regrab, it's the same release still sitting in the
                # queue's listing, and must not spuriously flip a settled `gone`/`cleaned` row
                # back to `detected`).
                if (
                    item["arr_status"] in _REMATCHABLE_STATES
                    and record.download_id is not None
                    and record.download_id == item["arr_download_id"]
                ):
                    continue
                matched = record
                break
            if matched is not None:
                used_record_ids.add(id(matched))
                await self._commit_match(queue_id, item, matched)

    async def _commit_match(self, queue_id: int, item: aiosqlite.Row, record: QueueRecord) -> None:
        is_regrab = item["arr_status"] in _REMATCHABLE_STATES
        await self.db.execute(
            "UPDATE item SET arr_status = 'detected', arr_status_at = ?, arr_download_id = ? "
            "WHERE id = ?",
            (_now_iso(), record.download_id, item["id"]),
        )
        await self.db.commit()
        message = (
            f"matched *arr queue record (downloadId={record.download_id!r}, "
            f"outputPath={record.output_path!r})"
        )
        if is_regrab:
            message += f" -- fresh association, prior state was {item['arr_status']!r} (regrab)"
        await audit.record_event(
            self.db, level="info", kind="arr_matched", item_id=item["id"], message=message
        )
        await self._publish_item(queue_id, item["id"])

    # --- Import/removal detection: detected|notified -> imported | gone ----------------------

    async def _check_import(
        self,
        client: ArrClient,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        records: list[QueueRecord],
    ) -> None:
        item_id = item["id"]
        download_id: str | None = item["arr_download_id"]

        if download_id is not None:
            current = next((r for r in records if r.download_id == download_id), None)
        else:
            # Defensive fallback for an association matched with no downloadId available (a
            # single-file release the title-fallback matched) -- name-based, same as matching
            # itself, per the spec's own "history lookup by name is fuzzy" acknowledgment.
            current = next((r for r in records if _record_matches_item(r, item["rel_path"])), None)

        # Requirement 1 (spec): "The queue record is gone (or reports
        # trackedDownloadState: imported)". Present with any other state -- including
        # `importing` -- means "not yet", full stop; the pending guard resets rather than
        # merely pausing, since a fresh two consecutive passes must observe both signals once
        # the record does leave.
        if (
            current is not None
            and current.tracked_download_state != TRACKED_DOWNLOAD_STATE_IMPORTED
        ):
            self._pending.pop(item_id, None)
            return

        # Requirement 2: >=1 history import event for the release.
        history: list[HistoryEvent] = await client.import_events(
            download_id=download_id,
            source_title=None if download_id else item["rel_path"],
        )
        has_import_event = any(e.is_import_event() for e in history)
        candidate_verdict: Literal["imported", "gone"] = "imported" if has_import_event else "gone"

        # Requirement 3: both signals held on two consecutive passes.
        prior = self._pending.get(item_id)
        if (
            prior is not None
            and prior.verdict == candidate_verdict
            and prior.download_id == download_id
        ):
            await self._commit_terminal(queue, item, candidate_verdict, len(history))
            self._pending.pop(item_id, None)
        else:
            self._pending[item_id] = _PendingVerdict(
                verdict=candidate_verdict, download_id=download_id
            )

    async def _commit_terminal(
        self,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        verdict: Literal["imported", "gone"],
        import_event_count: int,
    ) -> None:
        queue_id = queue["id"]
        await self.db.execute(
            "UPDATE item SET arr_status = ?, arr_status_at = ? WHERE id = ?",
            (verdict, _now_iso(), item["id"]),
        )
        await self.db.commit()
        if verdict == "imported":
            kind, message = (
                "arr_imported",
                f"*arr queue record gone/imported with {import_event_count} import history "
                "event(s), confirmed on two consecutive poller passes",
            )
        else:
            kind, message = (
                "arr_gone",
                "*arr queue record disappeared with no import history event, confirmed on two "
                "consecutive poller passes -- local files untouched, no cleanup performed",
            )
        await audit.record_event(
            self.db, level="info", kind=kind, item_id=item["id"], message=message
        )
        await self._publish_item(queue_id, item["id"])

        if verdict == "imported":
            # Rung 4 of the move-mode delete ladder (this module's own docstring) -- runs
            # before this pass's `arr_delete_completed` cleanup sweep (`_process_queue`'s own
            # ordering: `_check_import` -> here -> the cleanup loop), so "import green -> delete
            # source -> (optionally) delete local" holds even within a single poller pass.
            # Never called on `gone`.
            await self._maybe_delete_remote_on_import(queue, item["id"])

    # --- Rung 4 of the move-mode delete ladder (this module's own docstring, 2026-08-16) -----

    async def _maybe_delete_remote_on_import(self, queue: aiosqlite.Row, item_id: int) -> None:
        """`core/postprocess.py._maybe_delete_remote` defers a `move`-mode item's delete here
        (`item.remote_delete_pending` non-null) the moment it discovers, at the tail of its own
        pipeline run, that the item is *arr-tracked -- rungs 1-3 (completeness, verify, extract)
        had already cleared *then*, which is exactly what authorizes performing the delete now,
        unconditionally, once `_commit_terminal` confirms `imported`. No re-derivation of
        verify/extract state happens here: `remote_delete_pending` carries the verify evidence
        forward (`'VERIFIED'` or `'SKIPPED'`) so the eventual delete event reads exactly as
        informative as an immediate rung-3 delete's, via the same `perform_remote_delete`.

        A no-op, deliberately, for: a `copy`/`sync` queue (`remote_delete_pending` is never set
        for those); an item that was never deferred, including one `_maybe_delete_remote` found
        `CORRUPT`/`EXTRACT_FAILED` (that function clears the column on those branches rather
        than setting it, so "CORRUPT vetoes at every rung" holds all the way out here too); an
        item whose remote copy is already gone (`remote_deleted_at` set -- idempotent against a
        queue record briefly reappearing); and a process that never wired `remote_pool`/
        `host_provider` (a test fixture that doesn't exercise this feature -- the item simply
        stays deferred for a later pass, the same as a missing host in the immediate rung-3
        case).
        """
        if queue["sync_mode"] != "move" or self.remote_pool is None or self.host_provider is None:
            return

        cursor = await self.db.execute(
            "SELECT rel_path, remote_delete_pending, remote_deleted_at FROM item WHERE id = ?",
            (item_id,),
        )
        row = await cursor.fetchone()
        if (
            row is None
            or row["remote_delete_pending"] is None
            or row["remote_deleted_at"] is not None
        ):
            return

        queue_id = queue["id"]
        host = await self.host_provider()
        if host is None:
            await audit.record_event(
                self.db,
                level="error",
                item_id=item_id,
                kind="remote_delete_withheld",
                message=(
                    f"queue {queue_id} ({queue['name']!r}) mode=move: delete withheld -- "
                    "no host configured"
                ),
            )
            return

        remote_full = queue["remote_path"].rstrip("/") + "/" + row["rel_path"]
        ok = await perform_remote_delete(
            self.db,
            self.remote_pool,
            host,
            item_id=item_id,
            queue_id=queue_id,
            queue_name=queue["name"],
            remote_full=remote_full,
            verify_state=row["remote_delete_pending"],
        )
        if ok:
            await self.db.execute(
                "UPDATE item SET remote_delete_pending = NULL WHERE id = ?", (item_id,)
            )
            await self.db.commit()
        await self._publish_item(queue_id, item_id)

    # --- Publish (persist -> read back -> publish, DESIGN.md §2.2) ---------------------------

    async def _publish_item(self, queue_id: int, item_id: int) -> None:
        if self.events is None:
            return
        cursor = await self.db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        if row is None:
            return
        if row["state"] == "REMOVED_BOTH":
            # A row that has left both trees is out of the published projection
            # (`core/engine.py._project`'s rel_paths filter) and off the Files page. An
            # `item_delta` for it would resurrect a dead node in every connected client --
            # visible, un-actionable (no local copy to delete, no remote copy to queue), and
            # only cleared by the next connect-time snapshot. Seen live 2026-08-16: files
            # deleted by hand, then the *arr queue record removed, and the `gone` commit's
            # publish put both rows back on the page. The state write and audit event above
            # still happen; only the WS publish is skipped. (`REMOVED_LOCAL` still publishes:
            # its remote copy keeps it in the projection.)
            return
        self.events.publish({"type": "item_delta", "queue_id": queue_id, "nodes": [item_view(row)]})
