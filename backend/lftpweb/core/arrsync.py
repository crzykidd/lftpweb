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

**The rung-4 delete retries; it is no longer one-shot** (2026-08-17,
`prompts/done/2026-08-17-stranded-source-delete-retry.md`, live on both the user's test and
production systems: `SSH connection closed` on the deferred delete, `arr_cleanup` removing the
local copy anyway seconds later, and the resulting `REMOVED_LOCAL` row -- remote copy alive --
had no Delete affordance in the UI at all). Before this, the delete only ever fired once, from
`_commit_terminal`'s own `imported` transition; a transient SSH failure there stranded the
remote copy **permanently**, because nothing ever asked again. `_sweep_stranded_source_deletes`
now runs every pass, keyed off the debt itself (`item.remote_delete_pending IS NOT NULL`, a
terminal-import `arr_status`, `remote_deleted_at IS NULL`) rather than the transition that first
created it -- which is also what makes it a retroactive self-heal for a row already stranded
before this shipped, with no migration: the query alone matches it. Retries back off
per item (`_SourceDeleteRetryState`, the same growing-delay shape `_InstanceBackoff` above uses)
and pause after `MAX_SOURCE_DELETE_RETRY_ATTEMPTS`, writing one `remote_delete_retries_paused`
event rather than a `remote_delete_failed` every pass for as long as a seedbox stays down --
`remote_delete_pending` stays set throughout, so the manual Files-page delete (widened the same
day, see `frontend/src/lib/fileTree.ts.canDeleteLocal`) or a restart's clean in-memory slate can
still clear it. `_maybe_cleanup` also now withholds while a source delete is still owed
(`item.remote_delete_pending` non-null), restoring "delete source -> delete local" as an
enforced ladder order rather than a hoped-for one -- before this, cleanup ran regardless and the
local copy could vanish while the remote copy was still stranded, exactly what the production
incident above shows. And `_commit_terminal`'s `gone` branch now names a still-pending source
delete in its own event message (rung 4 never fires on `gone`, by design, unchanged) purely so
History can say why a source is still on the seedbox, without changing any behavior.

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
    command_outcome,
)
from lftpweb.core.arrnotify import notify_arr, translate_to_arr_namespace
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


def _derive_arr_root(output_path: str, item_name: str) -> str:
    """The *arr-side root a matched queue record's `outputPath` sits under -- `outputPath`
    itself minus the item's own trailing name segment (`_maybe_warn_path_mismatch`'s own
    docstring, 2026-08-17). Tolerates a trailing filename that doesn't literally equal
    `item_name` -- a single-file release matched via the normalized-title fallback
    (`_record_matches_item` above) can report any filename at all -- by falling back to a plain
    `dirname`; either way the intent is "the parent directory of whatever `outputPath` points
    at," the same root a notify's own translated push (`core/arrnotify.py.
    translate_to_arr_namespace`) must land under for the two to agree.
    """
    normalized = output_path.rstrip("/")
    suffix = "/" + item_name
    if normalized.endswith(suffix):
        return normalized[: -len(suffix)]
    return posixpath.dirname(normalized)


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

# Scan-command outcome verification (2026-08-17) -- how many poll passes `_check_scan_commands`
# will keep asking `GET /api/v3/command/{id}` about a command that never resolves to `completed`
# or `failed` before giving up silently (clearing `item.arr_scan_command_id`, no event). Bounds
# the per-pass API call this check costs to a handful of passes per pushed command, never
# forever -- a command that genuinely never resolves (the *arr restarted mid-run and lost its
# own command history, say) is exactly the same "no evidence either way" case a 404 already is.
MAX_SCAN_COMMAND_CHECK_ATTEMPTS = 5


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


# --- Rung-4 stranded-source-delete retry sweep (2026-08-17, this module's own docstring above,
# resolving the transient-SSH-failure gap) -----------------------------------------------------

# Bounded, same reasoning as `MAX_NOTIFY_RETRY_ATTEMPTS` above: an item whose delete keeps
# failing gets this many attempts, growing further apart each time (`INITIAL_BACKOFF_S`/
# `BACKOFF_FACTOR`/`MAX_BACKOFF_S`, reused from `_InstanceBackoff` above rather than
# reinvented), before this process pauses and writes one clear event rather than a
# `remote_delete_failed` every ~60s pass for as long as a seedbox stays down.
# `remote_delete_pending` is never cleared by pausing -- the manual Files-page delete or a
# restart's clean in-memory slate (`_SourceDeleteRetryState`'s own docstring) can still act.
MAX_SOURCE_DELETE_RETRY_ATTEMPTS = 5


@dataclass
class _SourceDeleteRetryState:
    """One item's rung-4 retry bookkeeping, present only once this process has tried and failed
    to clear its `remote_delete_pending` debt at least once. In-memory only, the same
    "restart loses it, and that's the safe direction" reasoning as `_pending`/`_notify_attempts`
    above -- a restart gets a clean slate and starts again from attempt 1, which is exactly the
    self-heal `_sweep_stranded_source_deletes` already guarantees on its very first pass, so
    losing this dict on restart costs nothing.
    """

    attempts: int
    next_attempt_at: float  # time.monotonic()
    paused: bool = False


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
        # Rung-4 stranded-source-delete retry backoff, keyed by item id -- see
        # `_SourceDeleteRetryState`'s own docstring for why in-memory is the right call here too.
        self._source_delete_retries: dict[int, _SourceDeleteRetryState] = {}
        # Namespace-mismatch warning debounce (2026-08-17, `_maybe_warn_path_mismatch`'s own
        # docstring) -- once per (queue id, derived *arr-side root) per process lifetime, the
        # same in-memory "restart loses it, and that's the safe direction" reasoning as every
        # other per-process dict above (a restart just re-warns once more, never silently).
        self._path_mismatch_warned: set[tuple[int, str]] = set()
        # Scan-command outcome check attempts, keyed by item id (2026-08-17,
        # `_check_scan_commands`'s own docstring) -- bounds the check to
        # `MAX_SCAN_COMMAND_CHECK_ATTEMPTS` passes, in memory: unlike `item.arr_scan_command_id`
        # itself (a persisted column, deliberately -- migration 021's own comment), losing this
        # counter on restart only means a slow-to-resolve command gets a few more free checks
        # than the bound technically allows, never fewer -- the same safe direction
        # `_notify_attempts` above already relies on.
        self._scan_command_checks: dict[int, int] = {}

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
        # `notify_on_complete` alongside the pre-existing columns (2026-08-17) -- the
        # namespace-mismatch check (`_maybe_warn_path_mismatch`) skips entirely when it's off,
        # the same "nothing will ever be pushed" reasoning `notify_arr`'s own
        # `"not_configured"` case already uses.
        cursor = await self.db.execute(
            "SELECT id, name, kind, base_url, api_key_enc, notify_on_complete "
            "FROM arr_instance WHERE enabled = 1"
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
                    await self._process_queue(
                        client,
                        queue,
                        records,
                        notify_on_complete=bool(instance["notify_on_complete"]),
                    )
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
        self,
        client: ArrClient,
        queue: aiosqlite.Row,
        records: list[QueueRecord],
        *,
        notify_on_complete: bool,
    ) -> None:
        queue_id = queue["id"]
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_status, arr_download_id, state, pending_download_prefix, "
            "remote_delete_pending FROM item WHERE queue_id = ? AND instr(rel_path, '/') = 0",
            (queue_id,),
        )
        items = await cursor.fetchall()

        await self._match_items(queue, items, records, notify_on_complete=notify_on_complete)

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

        # Rung-4 retry sweep (this module's own docstring, 2026-08-17) -- after the
        # import-check loop above (so a delete that finally clears this very pass is already
        # gone before the `arr_delete_completed` cleanup sweep below queries), keyed off the
        # debt itself rather than the `imported` transition, so a transient SSH failure gets
        # tried again next pass instead of stranding the remote copy permanently.
        await self._sweep_stranded_source_deletes(queue)

        # Scan-command outcome verification (2026-08-17) -- independent of the ladder/cleanup
        # work above, so its ordering relative to them doesn't matter; every item this queue is
        # still tracking a pushed command's outcome for (`item.arr_scan_command_id` non-null,
        # queried fresh inside), regardless of `arr_status`.
        await self._check_scan_commands(client, queue)

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

        if item["remote_delete_pending"] is not None:
            # Restores "delete source -> delete local" as an *enforced* ladder order rather
            # than a hoped-for one (2026-08-17, this module's own docstring) -- before this
            # check, cleanup ran regardless of the debt and could remove the local copy while
            # the remote copy was still stranded, exactly the production incident this task
            # fixes. `_sweep_stranded_source_deletes` retries the source delete every pass
            # (including this one, ahead of this cleanup sweep in `_process_queue`'s own
            # ordering), so the very next pass after it finally clears is the very next pass
            # cleanup is reconsidered here -- no extra timer, no state massaging. A `copy`
            # queue never sets `remote_delete_pending` (rung 4 is a `move`-only concept), so
            # this branch is a no-op there, matching the pre-existing behavior exactly.
            await self._record_cleanup_withheld(
                item_id,
                queue,
                "a deferred source delete is still pending (remote_delete_pending) -- "
                "ladder order requires delete source before delete local",
            )
            return

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
        self,
        queue: aiosqlite.Row,
        items: list[aiosqlite.Row],
        records: list[QueueRecord],
        *,
        notify_on_complete: bool,
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
                await self._commit_match(
                    queue, item, matched, notify_on_complete=notify_on_complete
                )

    async def _commit_match(
        self,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        record: QueueRecord,
        *,
        notify_on_complete: bool,
    ) -> None:
        queue_id = queue["id"]
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
        await self._maybe_warn_path_mismatch(
            queue, item, record, notify_on_complete=notify_on_complete
        )

    # --- Namespace-mismatch detection (2026-08-17, production evidence:
    # private_data/debug_logs/productionlftpweb.log) --------------------------------------------

    async def _maybe_warn_path_mismatch(
        self,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        record: QueueRecord,
        *,
        notify_on_complete: bool,
    ) -> None:
        """The user's *arr instances mount the same storage at a different path than lftpweb
        does (`/mnt/seanas02_media/Working/box-dc-tv` vs lftpweb's own
        `/mnt/seanas02-media-working/box-dc-tv`). With `arr_visible_path` unset, every notify
        pushed lftpweb's own path -- the *arr accepted the scan command (201) and silently
        scanned a directory that doesn't exist in *its* container, so imports waited on the
        *arr's own unrelated schedule instead of the push, and several associations drifted all
        the way to `gone`. The evidence to catch this was already in hand: the matched queue
        record's own `outputPath` is the *arr's view of this exact release, so a namespace
        mismatch is detectable the moment a match commits -- well before the first notify ever
        fires. `core/arrsync.py`'s own `arr_scan_command_failed` (a later addition, this same
        task) is the *confirmed* counterpart to this *predictive* one: this fires from a path
        comparison alone, before any push has even happened.

        **Detection only -- changes no behavior.** The notify still fires exactly as it does
        today (this is advisory, not a gate); a false positive -- an exotic remote-path-mapping
        setup where a mismatch is actually intentional -- costs one event, worded to allow for
        that.

        Skipped entirely, no event, in the cases where there is nothing to say: `record.
        output_path` is `None` (a title-fallback match has no *arr-side path to compare against
        at all), or `notify_on_complete` is off for this instance (nothing will ever be pushed,
        so a mismatch here is moot -- matches this module's "everything defaults off produces
        zero events, not noise" convention, same as `notify_arr`'s own `"not_configured"` case).

        Debounced once per `(queue id, derived *arr-side root)` per process lifetime
        (`self._path_mismatch_warned`) -- every subsequent match against the same misconfigured
        queue would otherwise repeat the identical advisory every poll pass for as long as the
        setting stays wrong.
        """
        if record.output_path is None or not notify_on_complete:
            return

        item_name = item["rel_path"]
        push_full = translate_to_arr_namespace(
            f"{queue['local_path'].rstrip('/')}/{item_name}",
            local_path=queue["local_path"],
            staging_path=queue["staging_path"],
            arr_visible_path=queue["arr_visible_path"],
        )
        push_root = posixpath.dirname(push_full.rstrip("/"))
        arr_root = _derive_arr_root(record.output_path, item_name)
        if push_root == arr_root:
            return

        debounce_key = (queue["id"], arr_root)
        if debounce_key in self._path_mismatch_warned:
            return
        self._path_mismatch_warned.add(debounce_key)

        await audit.record_event(
            self.db,
            level="warning",
            item_id=item["id"],
            kind="arr_path_mismatch",
            message=(
                f"queue {queue['id']} ({queue['name']!r}): a notify for this item would push "
                f"{push_root!r} but the *arr reports its own path for this release as "
                f"{record.output_path!r} (root {arr_root!r}) -- these look like different "
                "filesystem namespaces, so the push likely lands nowhere real on the *arr's "
                "side. If this is intentional (an unusual remote-path-mapping setup), ignore "
                f"this. Otherwise, set this queue's 'Path as seen by the *arr' to {arr_root!r}."
            ),
        )

    # --- Scan-command outcome verification (2026-08-17, production evidence:
    # private_data/debug_logs/productionlftpweb.log) -- `notify_arr`'s push was otherwise
    # fire-and-forget: a 201 only means "command queued", never "the *arr could act on this
    # path". `arr_scan_command_failed` below is the *confirmed* counterpart to
    # `arr_path_mismatch` above's *predictive* one -- that one fires from a path comparison
    # alone, before any push has happened; this one fires from the *arr's own eventual verdict
    # on a push that already went out. -----------------------------------------------------

    async def _check_scan_commands(self, client: ArrClient, queue: aiosqlite.Row) -> None:
        """Every item this queue is still tracking a pushed scan command's outcome for
        (`item.arr_scan_command_id` non-null, queried fresh -- independent of `arr_status`,
        since import can resolve before or after the command itself does) gets one
        `GET /api/v3/command/{id}` this pass. Persisted, not in-memory, because `notify_arr` is
        called from two different processes' objects (`core/postprocess.py`'s primary push,
        this module's own bounded notify-retry) -- see migration 021's own comment for why a
        restart must not orphan the check the way it safely orphans this module's other
        in-memory bookkeeping.
        """
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_scan_command_id FROM item "
            "WHERE queue_id = ? AND arr_scan_command_id IS NOT NULL",
            (queue["id"],),
        )
        for row in await cursor.fetchall():
            await self._check_one_scan_command(client, queue, row)

    async def _check_one_scan_command(
        self, client: ArrClient, queue: aiosqlite.Row, row: aiosqlite.Row
    ) -> None:
        item_id = row["id"]
        raw = await client.get_command(row["arr_scan_command_id"])

        if raw is None:
            # 404 -- pruned or unknown (the *arr prunes finished commands after a while, or
            # restarted and lost its own command history). No evidence either way, not a
            # failure: clear silently, same as a resolved outcome below.
            await self._clear_scan_command(item_id)
            self._scan_command_checks.pop(item_id, None)
            return

        outcome = command_outcome(raw)
        if outcome == "pending":
            attempts = self._scan_command_checks.get(item_id, 0) + 1
            if attempts >= MAX_SCAN_COMMAND_CHECK_ATTEMPTS:
                # Bounded, per this module's own docstring above -- never let a command that
                # genuinely never resolves accumulate a per-pass API call forever. Silent, like
                # the 404 case: this is "give up checking," not "the push failed."
                await self._clear_scan_command(item_id)
                self._scan_command_checks.pop(item_id, None)
                return
            self._scan_command_checks[item_id] = attempts
            return

        self._scan_command_checks.pop(item_id, None)
        await self._clear_scan_command(item_id)
        if outcome == "completed":
            # The push at least executed; import detection remains the authority on whether
            # anything actually got imported from it.
            return

        # "failed" -- the confirmed counterpart to `_maybe_warn_path_mismatch`'s predictive one.
        push_full = translate_to_arr_namespace(
            f"{queue['local_path'].rstrip('/')}/{row['rel_path']}",
            local_path=queue["local_path"],
            staging_path=queue["staging_path"],
            arr_visible_path=queue["arr_visible_path"],
        )
        await audit.record_event(
            self.db,
            level="warning",
            item_id=item_id,
            kind="arr_scan_command_failed",
            message=(
                f"queue {queue['id']} ({queue['name']!r}): the *arr scan command pushed for "
                f"{push_full!r} did not complete successfully -- if the *arr cannot see this "
                "path, set this queue's 'Path as seen by the *arr' to the path the *arr "
                "actually mounts this content under"
            ),
        )

    async def _clear_scan_command(self, item_id: int) -> None:
        await self.db.execute("UPDATE item SET arr_scan_command_id = NULL WHERE id = ?", (item_id,))
        await self.db.commit()

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
            if item["remote_delete_pending"] is not None:
                # Visibility only, no behavior change (2026-08-17, this module's own docstring)
                # -- rung 4 never fires on `gone` (by design: ambiguity must not trigger an
                # irreversible delete), so a deferred source delete that was still owed when the
                # *arr's queue record vanished just sits stranded silently otherwise. Production
                # evidence: 15 items went `notified` -> `gone` with `remote_delete_pending`
                # still set, and each source sat on the seedbox with nothing in History
                # explaining why.
                message += (
                    " -- a deferred source delete was still pending for this item; it remains "
                    "withheld (rung 4 never fires on `gone`, by design) -- manual deletion from "
                    "the Files page is the intended path"
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

    async def _maybe_delete_remote_on_import(self, queue: aiosqlite.Row, item_id: int) -> bool:
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

        **Called from two places now** (2026-08-17): `_commit_terminal`'s one-shot call on the
        `imported` transition (unchanged), and `_sweep_stranded_source_deletes`'s per-pass retry
        for a debt that first attempt failed to clear -- both share this one implementation,
        never a second one, same as the rest of this codebase's delete plumbing.

        Returns whether the debt is resolved by the time this call returns: `True` once
        `remote_deleted_at` is set (by this call or an earlier one) or there is genuinely
        nothing for this process to do about it (wrong sync mode, feature not wired, or the row
        already shows the debt cleared/deleted) -- none of those are a "failure" the retry sweep
        should back off on. `False` only for a real, still-outstanding failure (no host
        configured, or `perform_remote_delete` itself failed) that the sweep should retry again
        later.
        """
        if queue["sync_mode"] != "move" or self.remote_pool is None or self.host_provider is None:
            return True  # nothing this process will ever do about it -- not a failure to retry

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
            return True  # debt already resolved (or the item is gone) -- nothing to retry

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
            return False

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
        return ok

    async def _sweep_stranded_source_deletes(self, queue: aiosqlite.Row) -> None:
        """Retry sweep for rung 4's deferred source delete (2026-08-17, this module's own
        docstring). Re-asks `_maybe_delete_remote_on_import` every pass for every item this
        queue is still carrying a `remote_delete_pending` debt for -- keyed off the debt itself,
        not the one-shot `imported` transition that first created it, which is what turns a
        transient SSH failure (the production incident this task fixes) into something that
        gets tried again next pass instead of stranding the remote copy permanently.

        Keyed off the debt rather than the transition also means a row already stranded before
        this shipped -- `imported` or already `cleaned`, remote copy still alive,
        `remote_delete_pending` still set from the original one-shot attempt -- matches this
        same query and is retried on the very first pass after upgrade. No migration, no state
        massaging; the query alone is the self-heal. `arr_status IN ('imported', 'cleaned')`
        names both terminal-import outcomes explicitly (rather than just `'imported'`) for
        exactly that reason: `_maybe_cleanup`'s own new gate below means a *fresh* `cleaned` row
        can no longer carry a pending debt going forward, but a row that reached `cleaned` before
        this fix shipped already did, and still needs to be swept.

        Short-circuits before querying when the feature isn't wired this process
        (`remote_pool`/`host_provider` both `None`, true of most of this module's own tests) --
        the identical no-op `_maybe_delete_remote_on_import` itself falls back to, but skips the
        query and the backoff bookkeeping too, so an unwired fixture never accumulates state in
        `_source_delete_retries` it has no reason to.
        """
        if self.remote_pool is None or self.host_provider is None:
            return
        cursor = await self.db.execute(
            "SELECT id FROM item WHERE queue_id = ? AND remote_delete_pending IS NOT NULL "
            "AND arr_status IN ('imported', 'cleaned') AND remote_deleted_at IS NULL",
            (queue["id"],),
        )
        for row in await cursor.fetchall():
            await self._retry_stranded_source_delete(queue, row["id"])

    async def _retry_stranded_source_delete(self, queue: aiosqlite.Row, item_id: int) -> None:
        """One item's turn in the sweep above -- backoff bookkeeping around the shared
        `_maybe_delete_remote_on_import` call. Same growing-delay shape as `_InstanceBackoff`
        (module-level `INITIAL_BACKOFF_S`/`BACKOFF_FACTOR`/`MAX_BACKOFF_S`, reused rather than
        reinvented) but bounded at `MAX_SOURCE_DELETE_RETRY_ATTEMPTS`: past that, one
        `remote_delete_retries_paused` event fires -- not a `remote_delete_failed` every pass
        for as long as a seedbox stays down -- and this process stops trying.
        `remote_delete_pending` stays set throughout either way, so the manual Files-page delete
        or a restart's clean-slate sweep (`_SourceDeleteRetryState`'s own docstring) can still
        clear it.
        """
        state = self._source_delete_retries.get(item_id)
        if state is not None and (state.paused or time.monotonic() < state.next_attempt_at):
            return

        ok = await self._maybe_delete_remote_on_import(queue, item_id)
        if ok:
            self._source_delete_retries.pop(item_id, None)
            return

        attempts = (state.attempts if state is not None else 0) + 1
        if attempts >= MAX_SOURCE_DELETE_RETRY_ATTEMPTS:
            self._source_delete_retries[item_id] = _SourceDeleteRetryState(
                attempts=attempts, next_attempt_at=time.monotonic(), paused=True
            )
            await audit.record_event(
                self.db,
                level="warning",
                item_id=item_id,
                kind="remote_delete_retries_paused",
                message=(
                    f"queue {queue['id']} ({queue['name']!r}) mode=move: deferred source "
                    f"delete has failed {attempts} times -- pausing automatic retries; "
                    "remote_delete_pending stays set, so the manual Files-page delete or a "
                    "lftpweb restart's fresh sweep can still clear it"
                ),
            )
            return

        delay = min(INITIAL_BACKOFF_S * (BACKOFF_FACTOR ** (attempts - 1)), MAX_BACKOFF_S)
        self._source_delete_retries[item_id] = _SourceDeleteRetryState(
            attempts=attempts, next_attempt_at=time.monotonic() + delay
        )

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
