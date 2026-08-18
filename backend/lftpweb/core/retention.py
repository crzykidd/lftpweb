"""Local-file retention: the scheduled deletion of items past a retention window, plus
orphan lftp temp-file cleanup. Split out of `core/local_delete.py` (audit P3); calls the
shared `delete_local` primitive there, never its own deletion path."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import audit, download_prefix, extract, local_scan, mount_sentinel
from lftpweb.core.events import EventBus
from lftpweb.core.local_delete import DeleteInFlight, delete_local

logger = logging.getLogger(__name__)

SETTING_KEY = "retention_settings"


@dataclass(frozen=True)
class RetentionSettings:
    """Site-level toggle. **Default off, non-negotiable** -- this project ships new
    capabilities off, and deletion of the user's own data is not where to make the one
    reasoned exception (scheduled backups) this project has made: backups add files, this
    removes them.
    """

    enabled: bool = False
    retention_days: float = 30.0


async def load_retention_settings(db: aiosqlite.Connection) -> RetentionSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return RetentionSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return RetentionSettings()
    return RetentionSettings(
        enabled=bool(data.get("enabled", False)),
        retention_days=float(data.get("retention_days", 30.0)),
    )


async def save_retention_settings(db: aiosqlite.Connection, settings: RetentionSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            SETTING_KEY,
            json.dumps({"enabled": settings.enabled, "retention_days": settings.retention_days}),
        ),
    )
    await db.commit()


async def _select_expired(
    db: aiosqlite.Connection, retention_days: float
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every top-level item (DESIGN.md §4.7's granularity, the same one
    `core/autoqueue.py`/`core/settle.py` use) whose `downloaded_at` is older than
    `retention_days` -- **not `state_changed_at`**: "when did this complete" and "when did it
    last move" are different questions, and an item that dips `DOWNLOADED -> PARTIAL ->
    DOWNLOADED` must not earn a fresh lease it hasn't actually earned
    (`prompts/open-issues.md`). `downloaded_at` is backfilled for the reconcile-only path by
    `core/engine.py._persist` (2026-08-12) precisely so this query doesn't silently skip a
    large share of the library.

    Restricted to `mount_sentinel.COMPLETE_STATES` -- the item must currently be in a state
    that asserts its bytes are all here; a `PARTIAL` or already-`REMOVED_LOCAL` item has
    nothing retention should be acting on -- and to `auto_queue_suppressed = 0`, so an item a
    previous retention pass (or a manual delete) already removed is never selected again.
    Only enabled queues are considered, mirroring `core/engine.py.scan_all`'s own `if not
    q.enabled: continue`.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    states_sql = ",".join("?" for _ in mount_sentinel.COMPLETE_STATES)
    cursor = await db.execute(
        "SELECT item.id AS item_id, item.rel_path AS item_rel_path, item.local_size AS item_local_size, "
        "item.downloaded_at AS item_downloaded_at, item.state AS item_state, "
        "path_queue.id AS queue_id, path_queue.name AS queue_name, "
        "path_queue.local_path AS queue_local_path "
        "FROM item JOIN path_queue ON path_queue.id = item.queue_id "
        "WHERE item.downloaded_at IS NOT NULL AND item.downloaded_at < ? "
        "AND item.auto_queue_suppressed = 0 AND instr(item.rel_path, '/') = 0 "
        f"AND item.state IN ({states_sql}) AND path_queue.enabled = 1 "  # noqa: S608 - fixed set, no user input
        "ORDER BY item.downloaded_at ASC",
        (cutoff, *mount_sentinel.COMPLETE_STATES),
    )
    rows = await cursor.fetchall()
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        item = {
            "id": row["item_id"],
            "rel_path": row["item_rel_path"],
            "local_size": row["item_local_size"],
            "downloaded_at": row["item_downloaded_at"],
            "state": row["item_state"],
        }
        queue = {
            "id": row["queue_id"],
            "name": row["queue_name"],
            "local_path": row["queue_local_path"],
        }
        out.append((item, queue))
    return out


async def preview_retention(
    db: aiosqlite.Connection,
    *,
    retention_days: float,
    in_flight_item_ids: frozenset[int],
) -> list[dict[str, Any]]:
    """ "Here is exactly what would be deleted, and the total bytes" -- the dry-run endpoint
    (prompts/open-issues.md "7 + 8"), mirroring `api/settings.py.pattern_preview`'s idiom
    rather than inventing a new one. Runs `delete_local(dry_run=True)` for every candidate with
    the guard shape a real retention pass uses (`require_nlink_guard=True`), so this is
    provably "what a real run would delete," not a separately-maintained approximation of it.
    """
    candidates = await _select_expired(db, retention_days)
    out: list[dict[str, Any]] = []
    for item, queue in candidates:
        outcome = await delete_local(
            db,
            item=item,
            queue=queue,
            caller="retention",
            require_nlink_guard=True,
            in_flight_item_ids=in_flight_item_ids,
            dry_run=True,
        )
        if outcome.deleted:
            out.append(
                {
                    "item_id": item["id"],
                    "queue_id": queue["id"],
                    "queue_name": queue["name"],
                    "rel_path": item["rel_path"],
                    "local_size": item["local_size"],
                    "downloaded_at": item["downloaded_at"],
                }
            )
    return out


# --- Orphaned temp-file cleanup (2026-08-13, prompts/2026-08-13-lftp-timestamped-temp-files.md) -

ORPHAN_TEMP_CLEANUP_SETTING_KEY = "orphan_temp_cleanup_settings"


@dataclass(frozen=True)
class OrphanTempCleanupSettings:
    """Site-level toggle for `local_scan.sweep_orphan_temp_files`. **Default off**, this
    project's rule for anything that deletes (`RetentionSettings`'s own docstring), even though
    the thing being removed here is accidental byte waste with no diagnostic value: an operator
    who has never hit the duplicate-job bug this task fixes (`core/queue.py.enqueue_item`) has
    no orphaned temp files to clean up, and the first time this runs should still be a decision,
    not a surprise sweep of a directory someone is mid-transfer into with an unusually long
    stall.
    """

    enabled: bool = False
    max_age_days: float = local_scan.ORPHAN_TEMP_FILE_DEFAULT_MAX_AGE_DAYS


async def load_orphan_temp_cleanup_settings(db: aiosqlite.Connection) -> OrphanTempCleanupSettings:
    cursor = await db.execute(
        "SELECT value FROM setting WHERE key = ?", (ORPHAN_TEMP_CLEANUP_SETTING_KEY,)
    )
    row = await cursor.fetchone()
    if row is None:
        return OrphanTempCleanupSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return OrphanTempCleanupSettings()
    return OrphanTempCleanupSettings(
        enabled=bool(data.get("enabled", False)),
        max_age_days=float(
            data.get("max_age_days", local_scan.ORPHAN_TEMP_FILE_DEFAULT_MAX_AGE_DAYS)
        ),
    )


async def save_orphan_temp_cleanup_settings(
    db: aiosqlite.Connection, settings: OrphanTempCleanupSettings
) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            ORPHAN_TEMP_CLEANUP_SETTING_KEY,
            json.dumps({"enabled": settings.enabled, "max_age_days": settings.max_age_days}),
        ),
    )
    await db.commit()


@dataclass(frozen=True)
class RetentionRunResult:
    considered: int
    deleted: int
    withheld: int


class RetentionScheduler:
    """Background loop, same `_task`/`start()`/`stop()` shape as `core/backup.py.
    BackupScheduler`, `core/engine.py.Engine`, and `core/metrics.py.MetricsRetentionScheduler`
    -- one bad cycle must not kill the loop, and `stop()` cancels cleanly on shutdown.

    `in_flight_provider` is a zero-arg callable returning `PostprocessPipeline.
    in_flight_item_ids()` at call time, the same plain-attribute-after-construction seam
    `core/engine.py.Engine.postprocess` uses -- `main.py`'s lifespan can't hand this scheduler
    a `PostprocessPipeline` instance until one exists, and this module must not import
    `core/postprocess.py` just to type a callable.
    """

    CHECK_INTERVAL_S = 3600.0  # hourly, same cadence as MetricsRetentionScheduler/BackupScheduler

    def __init__(
        self,
        db: aiosqlite.Connection,
        events: EventBus,
        *,
        in_flight_provider: Callable[[], frozenset[int]] | None = None,
        delete_in_flight: DeleteInFlight | None = None,
    ) -> None:
        self.db = db
        self.events = events
        self._in_flight_provider = in_flight_provider or (lambda: frozenset())
        # 2026-08-13: the same `DeleteInFlight` instance the manual delete endpoint uses
        # (`api/jobs.py`), threaded through so a scheduled retention delete gets the identical
        # transient-state/scan-protection treatment as a manual one -- see `delete_local`'s own
        # docstring. `None` (every existing caller/test) simply means retention deletes get no
        # extra protection, exactly the pre-existing behavior.
        self._delete_in_flight = delete_in_flight
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-retention-loop")

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
                logger.exception("retention cycle failed")
            await asyncio.sleep(self.CHECK_INTERVAL_S)

    async def run_once(self) -> RetentionRunResult:
        # 2026-08-13: orphan temp-file cleanup rides the same hourly pass but is gated by its
        # own, independent setting -- an operator who wants one without the other (most will:
        # this is disk hygiene from a bug, not a data-lifecycle policy) must not have to accept
        # both or neither.
        await self._sweep_orphan_temp_files()
        await self._sweep_orphan_extract_debris()

        settings = await load_retention_settings(self.db)
        if not settings.enabled:
            return RetentionRunResult(considered=0, deleted=0, withheld=0)

        candidates = await _select_expired(self.db, settings.retention_days)
        deleted = withheld = 0
        for item, queue in candidates:
            outcome = await delete_local(
                self.db,
                item=item,
                queue=queue,
                caller="retention",
                require_nlink_guard=True,
                in_flight_item_ids=self._in_flight_provider(),
                events=self.events,
                delete_in_flight=self._delete_in_flight,
            )
            if outcome.deleted:
                deleted += 1
            else:
                withheld += 1
        if candidates:
            logger.info(
                "retention: considered %d item(s), deleted %d, withheld %d",
                len(candidates),
                deleted,
                withheld,
            )
        return RetentionRunResult(considered=len(candidates), deleted=deleted, withheld=withheld)

    async def _sweep_orphan_temp_files(self) -> None:
        """Disk-hygiene half of prompts/2026-08-13-lftp-timestamped-temp-files.md's task 4 --
        removes stale `.lftp`/`.lftp~<timestamp>~` leftovers (`local_scan.
        sweep_orphan_temp_files`'s own docstring covers why age alone is a safe guard) across
        every enabled queue, gated by `OrphanTempCleanupSettings.enabled` (default off, this
        module's own rule for anything that deletes).

        One queue at a time, off the event loop (`asyncio.to_thread`, matching
        `postprocess.py._sweep_failed_dirs`'s reasoning for `extract.sweep_failed_dirs`: a
        large tree walk must not block request handling or the WS delivery of anything else
        happening at the same time). No `item_delta` publish here -- unlike `delete_local`,
        an orphaned temp file was never its own visible node (`core/local_scan.py.scan_local`
        already folds it into its final name's entry, dead or alive), so there is no row on the
        Files page whose removal needs announcing; the `event` row is the whole audit trail.
        """
        settings = await load_orphan_temp_cleanup_settings(self.db)
        if not settings.enabled:
            return
        cursor = await self.db.execute("SELECT id, local_path FROM path_queue WHERE enabled = 1")
        queues = await cursor.fetchall()
        for queue in queues:
            removed = await asyncio.to_thread(
                local_scan.sweep_orphan_temp_files,
                Path(queue["local_path"].rstrip("/")),
                max_age_days=settings.max_age_days,
            )
            for path, age_days in removed:
                await audit.record_event(
                    self.db,
                    level="info",
                    kind="orphan_temp_file_removed",
                    message=(
                        f"queue {queue['id']}: removed stale lftp temp file {path} "
                        f"(age {age_days:.1f}d >= {settings.max_age_days}d)"
                    ),
                )
            if removed:
                logger.info(
                    "orphan-temp-cleanup: removed %d stale temp file(s) in queue %d",
                    len(removed),
                    queue["id"],
                )

    async def _sweep_orphan_extract_debris(self) -> None:
        """Remove a top-level `_FAILED_`/`_UNPACK_` extraction-staging directory once its
        owning item has left tracking entirely (2026-08-18,
        `prompts/done/2026-08-18-sweep-orphaned-extract-debris.md`; production find: a
        `_FAILED_.downloading-<name>` directory outlived its item's manual delete forever --
        both prefixes are filtered out of `core/local_scan.py`'s walk by design, so it had no
        item row, no UI presence, and nothing that would ever clean it up).

        **Widens `PostprocessSettings.failed_retention_enabled` / `extract.sweep_failed_dirs`'s
        existing bounded-lifetime mechanism (2026-08-12), not a second, parallel one.** That
        sweep is age-only and runs only from inside `PostprocessPipeline._do_extract` -- i.e.
        only when *another* extraction attempt happens to touch the same queue. A `_FAILED_`/
        `_UNPACK_` directory whose owning item was deleted or `Reset item tracking`'d never
        triggers another extraction at that name again, so that sweep's own trigger for it can
        never fire -- however long `failed_retention_days` is set to, and it also only ever
        covers `_FAILED_`, never `_UNPACK_`. This pass widens coverage to exactly that gap:
        "the owning item is gone from tracking altogether," a condition age can't express and
        the existing sweep never checks.

        **Deliberately not gated by a settings toggle**, unlike `OrphanTempCleanupSettings` and
        `failed_retention_enabled` above -- see `docs/decisions.md` for the full reasoning. In
        short: those two toggles gate a *policy* choice (how long to keep diagnostic evidence
        around, or how aggressively to reap accidental byte waste); this only ever acts once the
        owning item is *provably* gone from the `item` table too (no row at all, or the row
        itself already reads `REMOVED_BOTH`) -- at that point the directory has no Files-page
        row, no delete affordance, and no future use to anyone, so withholding it behind an
        off-by-default toggle would just mean a default install keeps leaking exactly the disk
        the production incident found. It stays extremely narrow to earn that: an owning item
        in *any* other state (including `REMOVED_LOCAL`, where a remote copy could still come
        back) or currently in flight leaves the directory untouched.

        **Mount-sentinel gated, per queue, load-bearing.** `core/mount_sentinel.py.check`'s own
        docstring: an empty directory and an unmounted share are indistinguishable by content
        alone. If a queue's `local_path` is a dropped mount that happens to expose some *other*
        (bare, unmounted) filesystem underneath, every item this scheduler actually knows about
        would correctly read "no matching row" against whatever unrelated content sits there --
        which would misread as "everything is orphaned" and sweep debris that has nothing to do
        with this queue at all. A queue that fails the sentinel check is skipped **entirely**
        for this pass, the same rail `core/autoqueue.py.on_scan` and `local_delete.delete_local`
        already apply before acting on anything.
        """
        cursor = await self.db.execute("SELECT id, local_path FROM path_queue WHERE enabled = 1")
        queues = await cursor.fetchall()
        for queue in queues:
            local_path = queue["local_path"].rstrip("/")
            if not mount_sentinel.check(local_path):
                continue
            debris_dirs = await asyncio.to_thread(
                extract.list_top_level_debris_dirs, Path(local_path)
            )
            for path in debris_dirs:
                await self._maybe_remove_debris_dir(queue["id"], path)

    async def _maybe_remove_debris_dir(self, queue_id: int, path: Path) -> None:
        """One `extract.list_top_level_debris_dirs` candidate: derive its owning item's logical
        name(s) (`_derive_debris_owner_candidates`), decide whether that owner is still live,
        and remove the directory only when it provably is not. See
        `_sweep_orphan_extract_debris`'s own docstring for the exact liveness rule.
        """
        candidates = _derive_debris_owner_candidates(path.name)
        if not candidates:
            return  # matched a prefix but stripped to an empty name -- nothing to key off

        in_flight_ids = self._in_flight_provider()
        delete_in_flight_ids = (
            self._delete_in_flight.in_flight_item_ids()
            if self._delete_in_flight is not None
            else frozenset()
        )

        orphan_item_id: int | None = None
        orphan_name: str | None = None
        for name in candidates:
            cursor = await self.db.execute(
                "SELECT id, state FROM item WHERE queue_id = ? AND rel_path = ?",
                (queue_id, name),
            )
            row = await cursor.fetchone()
            if row is None:
                continue
            if row["id"] in in_flight_ids or row["id"] in delete_in_flight_ids:
                return  # owning item's extract/delete worker is in flight -- never sweep under it
            if row["state"] != "REMOVED_BOTH":
                return  # owning item is still live (any state short of REMOVED_BOTH) -- leave it
            orphan_item_id, orphan_name = row["id"], name

        derived_name = orphan_name if orphan_name is not None else candidates[0]
        reason = (
            "owning item's row reads REMOVED_BOTH (left both trees)"
            if orphan_item_id is not None
            else "no item row matches the derived name -- manually deleted or reset"
        )
        removed = await asyncio.to_thread(_rmtree_if_still_a_dir, path)
        if not removed:
            return
        await audit.record_event(
            self.db,
            level="info",
            item_id=orphan_item_id,
            kind="extract_debris_removed",
            message=(
                f"queue {queue_id}: removed orphaned extraction staging/evidence directory "
                f"{path} (derived item name {derived_name!r}; {reason})"
            ),
        )
        logger.info(
            "extract-debris-sweep: removed orphaned %s (queue %d, derived item %r)",
            path,
            queue_id,
            derived_name,
        )


def _derive_debris_owner_candidates(dir_name: str) -> tuple[str, ...]:
    """The owning item's logical `rel_path`, derived from a top-level `_FAILED_<name>`/
    `_UNPACK_<name>` staging directory's own name (2026-08-18,
    `prompts/done/2026-08-18-sweep-orphaned-extract-debris.md`) -- strip the staging prefix,
    then strip a leading `.downloading-` if present (the production incident's exact shape:
    `_FAILED_.downloading-Hard.Knocks...` resolves to item `Hard.Knocks...`, not
    `.downloading-Hard.Knocks...`).

    Returns up to two candidates, most-stripped first, so the caller can check *both* against
    the `item` table and only ever treat the directory as orphaned when neither resolves to a
    live row -- ambiguous is "leave it alone," never "guess and delete." Only the *default*
    download prefix (`core/download_prefix.py.DEFAULT_PREFIX`) is recognised here; a queue
    running a custom prefix does not get a second candidate. Named limitation, not a silent one
    (docs/decisions.md): unlike `core/postprocess.py._find_item_id_for_failed_dir`, which can
    recover a *live* item's actual physical prefix from its own persisted
    `pending_download_prefix` column, there is by construction no row left to read that column
    off in the "no item row matches at all" case this sweep exists for.

    Returns an empty tuple if `dir_name` doesn't start with either staging prefix at all, or
    strips to nothing -- both mean "not enough to key off," never a candidate.
    """
    for prefix in (extract.UNPACK_PREFIX, extract.FAILED_PREFIX):
        if not dir_name.startswith(prefix):
            continue
        rest = dir_name[len(prefix) :]
        if not rest:
            return ()
        if rest.startswith(download_prefix.DEFAULT_PREFIX):
            stripped = rest[len(download_prefix.DEFAULT_PREFIX) :]
            if stripped:
                return (stripped, rest)
        return (rest,)
    return ()


def _rmtree_if_still_a_dir(path: Path) -> bool:
    """The actual filesystem removal, off the event loop (`asyncio.to_thread`, matching every
    other tree-walk/removal in this module). Re-checks `is_dir()` immediately before acting --
    belt-and-suspenders against the async gap between `extract.list_top_level_debris_dirs`
    listing this path and the DB round-trips `_maybe_remove_debris_dir` makes before reaching
    here; containment itself was already verified once, synchronously, inside the listing call,
    which is the one containment check this codebase deletes disk content through
    (`extract.resolve_within_root`'s own docstring).
    """
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True


# --- Delete archives after extract (prompts/2026-08-13-delete-archives-after-extract.md) ------
#
# **The trap this section exists to avoid.** Deleting a release's archive volumes after a
# successful extraction drops the item's local byte total below its remote total. The next scan
# (`core/reconcile.py`) would read that as `local < remote` -> `PARTIAL` (DESIGN.md §3.2 rule
# 2), and rule 9/`core/postprocess.py.outcome_survives_rescan` says `PARTIAL` beats any
# post-processing outcome -- so the `EXTRACTED` state this codebase just wrote would not
# protect the item, and auto-queue would re-fetch the archives, extract them again, delete them
# again, every scan interval, forever. This is the same shape as the `REMOVED_LOCAL` bug shipped
# and reverted the same night in `6d3bd95` (prompts/open-issues.md "4").
#
# The fix is **not** a new completeness rule and **not** `auto_queue_suppressed` (that flag is
# for user decisions and permanent errors, DESIGN.md §4.6, and suppressing an item here would
# also stop it being legitimately re-fetched if the user ever wanted it again). It reuses the
# mechanism `core/patterns.py.build_counts_predicate` already built for the identical problem
# with a different cause (a `file_exclude` pattern instead of a deletion this codebase
# performed): a file the counts_predicate rejects is marked `EXCLUDED` -- a real state, not an
# absence -- and stops counting toward its parent directory's completeness (DESIGN.md §3.2 rule
# 8). `load_deleted_archive_paths`/`save_deleted_archive_paths` below persist exactly the set
# `core/engine.py.scan_queue` needs to fold into that same seam; see that function for how the
# two sources (patterns, deletions) combine into one predicate.


# No site setting lives in this module for this feature -- see
# `core/postprocess.py.PostprocessSettings.delete_archives_after_extract`, alongside the other
# post-processing toggles. This module only holds the deletion primitive and its bookkeeping
# table, the same split `retention_settings` above doesn't need because retention's own toggle
# has no other natural home.
