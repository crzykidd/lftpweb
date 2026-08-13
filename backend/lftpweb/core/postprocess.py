"""Post-processing pipeline: verify, extract, staging move (DESIGN.md §6) -- plus the
`move`-mode remote delete this phase adds on top of it (§7, §7.4).

**Trigger.** Two call sites, both narrow, neither a general "scan found DOWNLOADED" hook.
`core/queue.py._reap_one`'s job-success path calls `PostprocessPipeline.trigger` the moment a
*top-level* item (no `/` in `rel_path` -- the same eligibility shape `core/autoqueue.py` uses)
transitions to `DOWNLOADED`. `core/engine.py._persist` calls it too, but only for a `rel_path`
its own settle-gate bookkeeping (prompts/open-issues.md #2) just released straight from
`REMOTE_ONLY`/`substate='settling'` to `DOWNLOADED` with no fresh job in between -- the
stuck-item bug this second trigger fixes: a job can finish while its item is still unsettled,
`_reap_one` holds it rather than calling `trigger` itself, and without this second call site
nothing else ever un-wedges it once the remote goes quiet and auto-queue is off. Both call
sites fire on the identical precondition (`item.state` about to become `DOWNLOADED`, no
post-processing outcome yet), so this is not a scan-driven trigger for the general case -- a
pre-existing local file that reads `DOWNLOADED` on its very first-ever scan, with no gate hold
behind it, still triggers nothing, exactly as before this widening. DESIGN.md §6 currently
describes only the first call site and needs a follow-up correction; see docs/decisions.md
(this task's entry) for the drafted wording. See docs/decisions.md more generally for why the
second path was rejected once already and is now built anyway.

**Runs off the event loop.** Hashing, `7zz`, and file copies are all blocking I/O; every step
calls into `core/verify.py` / `core/extract.py` / `move_tree` via `asyncio.to_thread`.

**Every step defaults off, at two independent layers** (DESIGN.md §6: "toggleable globally
and per path queue"). A step runs for an item only when *both* `PostprocessSettings`'s own
flag and the queue's `auto_verify`/`auto_extract`/`auto_move` column are true -- except
verification for a `move`-mode queue, which always runs regardless of either toggle, because
it is the sole gate on an irreversible remote delete (see the decision recorded in
`docs/decisions.md`: muting it via an unrelated global switch would silently turn `move` into
"downloads, never deletes, never says why").

**Deletion (§7.4).** `move` deletes the remote copy only after verification returns
`VERIFIED` -- never `CORRUPT`, never `SKIPPED` (no evidence). Every delete and every withheld
delete writes an `event` row (`core/audit.py`) naming the item, the queue, the mode, and the
gating condition. Deletion itself always goes through `core/remote.py`'s pooled asyncssh
connection (`RemoteConnectionPool.delete_path`), never lftp's `--Remove-source-files`.

**The staging move and `REMOVED_LOCAL`.** After a successful move-to-staging, this module
does *not* set a new item state -- it deliberately reuses the machinery phase 4 already built
for exactly this shape (`core/mount_sentinel.py`'s grace-period `REMOVED_LOCAL` transition):
the next scan finds the item's local copy gone from `local_path`, and since the item was
`DOWNLOADED`/`VERIFIED`/`EXTRACTED`, it is indistinguishable -- correctly -- from a human or
an `*arr` importer having moved it out by hand. See `docs/decisions.md` for the field-naming
question this resolves (`local_path` vs `staging_path`).
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite

from lftpweb.core import audit, extract, verify
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view
from lftpweb.core.remote import HostConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- The states this module owns (DESIGN.md §3.2, §6) ----------------------------------------
#
# This module is the *only* writer of these six states, the same way `core/queue.py` is the
# only writer of QUEUED/DOWNLOADING/STOPPED/FAILED and `core/reconcile.py` is the only
# producer of the structural ones. They're named here, rather than restated as string
# literals wherever they're needed, because two other modules have to reason about them and
# a drifting second list would be silent: `core/engine.py._persist` (whose write must not
# stomp them) and `core/mount_sentinel.py` (whose grace period must still be able to carry
# them to REMOVED_LOCAL). Both import from here; nothing here imports either of them.

# Held only while a worker is actually mid-run. Nothing else in the system may infer anything
# from them -- in particular, a row still carrying one of these after a restart means the
# worker died, not that work is in progress (see `in_flight_item_ids` for how that resolves).
TRANSIENT_STATES = frozenset({"VERIFYING", "EXTRACTING"})

# Outcomes. These must survive: `CORRUPT` and `EXTRACT_FAILED` are the two states a user most
# needs to still be there when they next look at the page, and DESIGN.md §6 ("failures are
# recorded on the item") is only true if a rescan can't quietly erase them.
TERMINAL_STATES = frozenset({"VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED"})

OWNED_STATES = TRANSIENT_STATES | TERMINAL_STATES


def outcome_survives_rescan(
    prev_state: str | None,
    structural_state: str,
    *,
    remote_deleted_at: str | None = None,
) -> bool:
    """Whether a persisted post-processing *outcome* must win over the structural state
    `core/reconcile.py` just recomputed -- the "content is still present" half of the
    precedence rule (`core/engine.py._persist` is the only caller; the "content has gone
    absent" half is `core/mount_sentinel.py.resolve_absence`).

    `VERIFIED`/`CORRUPT`/`EXTRACTED`/`EXTRACT_FAILED` are **refinements of `DOWNLOADED`**:
    each one says something about an item whose bytes are all here that the byte comparison
    itself cannot say. So an outcome wins over a fresh `DOWNLOADED` --

    - `PARTIAL` (§3.2 rule 2) wins over the outcome instead. Local is short of remote again --
      the remote grew (rule 4), or something took files away -- so the item is genuinely
      re-queueable, and "VERIFIED" would be a claim about content that is no longer there.
      Rule 2's "never DOWNLOADED" is absolute, and an outcome is a stronger claim still.
    - `REMOTE_ONLY` is absence, which is *not* decided here: it goes to `resolve_absence`, so
      an item whose local copy was moved out by an importer still reaches `REMOVED_LOCAL`
      through §7.3's grace period rather than being frozen on its outcome forever.

    Transient states deliberately don't survive on this path. Protecting them is the job of
    `in_flight_item_ids()`, which is true only while a worker really is running -- so a state
    left behind by a crashed one is recomputed away on the next scan instead of wedging.

    **...and, narrowly, over `LOCAL_ONLY` too -- but only when `remote_deleted_at` is set**
    (fix, 2026-08-13, `prompts/2026-08-13-move-mode-outcome-survives-local-only.md`, found the
    first time `move` mode ran end to end: verify -> delete the remote -> extract all
    succeeded, and every item read `LOCAL_ONLY` within one scan interval anyway).
    `core/reconcile.py` reads "remote absent, local present" as `LOCAL_ONLY` -- correct for a
    file that was genuinely never tracked remotely, but exactly what a `move`-mode item's own
    remote copy looks like the scan *after* this module deleted it on purpose
    (`_maybe_delete_remote`). `remote_deleted_at` is the signal that tells the two apart: it is
    set only when *this codebase* removed the remote copy, after verification, so a
    `LOCAL_ONLY` reading alongside it means "the bytes are all here and the remote is gone
    because we deleted it" -- a refinement of the outcome in exactly the sense `DOWNLOADED` is,
    not a fresh, never-tracked local file. An item that is genuinely `LOCAL_ONLY` for any other
    reason has `remote_deleted_at IS NULL` and this branch never fires for it.

    Deliberately **not** a blanket "any outcome survives while `remote_deleted_at` is set,
    forever": this only fires while `structural_state == "LOCAL_ONLY"`, i.e. while local
    content is still actually present. Once that content is *also* gone -- `_do_move`
    relocated it, or something else took it -- the path leaves both trees entirely and this
    function is never even reached for it; `core/engine.py._persist`'s own "vanished from both
    trees" handling is what lets §7.3's grace period still carry it to `REMOVED_LOCAL` rather
    than freezing it on its outcome forever (see that method for why one isn't a substitute for
    the other).
    """
    if prev_state not in TERMINAL_STATES:
        return False
    if structural_state == "DOWNLOADED":
        return True
    return structural_state == "LOCAL_ONLY" and remote_deleted_at is not None


# --- Settings (JSON in `setting`, the same pattern core/queue.py.TransferSettings uses) -----

SETTING_KEY = "postprocess_settings"


@dataclass(frozen=True)
class PostprocessSettings:
    """Site-level defaults for the pipeline (DESIGN.md §6). Every step defaults off; the
    per-queue `auto_verify`/`auto_extract`/`auto_move` columns are the other half of the
    "toggleable globally and per path queue" requirement -- see the module docstring for how
    the two combine.
    """

    verify_enabled: bool = False
    # No sidecar found -> whole-file read as a weaker verification (proves readability, not
    # content correctness -- see core/verify.py's module docstring). Off by default: a
    # queue with no .sfv/.md5 evidence should say so loudly (DESIGN.md §6), not quietly
    # promote a size-only completeness check to "verified."
    verify_hash_on_disk: bool = False
    extract_enabled: bool = False
    # None = in place (each archive's own containing directory). DESIGN.md §6 doesn't say
    # where this configuration lives (queue or site); see docs/decisions.md for why it's
    # site-level here, like bandwidth/concurrency (§4.5) rather than per-queue.
    extract_target_dir: str | None = None
    extract_passwords: tuple[str, ...] = ()
    # `_FAILED_` staging directories (core/extract.py) are kept as diagnostic evidence
    # forever unless this sweeps them -- fix, 2026-08-12 (docs/decisions.md). Off by default
    # on this project's own rule ("a new capability defaults off, and deletion is not where
    # to make an exception") even though the sweep's own containment check
    # (`extract.sweep_failed_dirs`) is deliberately conservative: only a direct child of the
    # queue's `local_path` whose name starts with `_FAILED_` is ever a candidate.
    failed_retention_enabled: bool = False
    failed_retention_days: float = extract.FAILED_RETENTION_DEFAULT_DAYS
    # Delete an item's spent archive volumes once they've extracted successfully (2026-08-13,
    # `prompts/2026-08-13-delete-archives-after-extract.md`). Off by default -- this project's
    # rule for anything that deletes, same as `failed_retention_enabled` above and
    # `local_delete.RetentionSettings` -- even though the naive infinite-re-download trap this
    # guards against (see `core/local_delete.py.delete_extracted_archives`'s module-level
    # comment) is already closed regardless of this flag; the flag exists because deleting the
    # user's own files is not a decision this project makes for them.
    delete_archives_after_extract: bool = False
    move_enabled: bool = False
    # DESIGN.md §6: "executed in a thread pool, one item at a time by default (configurable)."
    concurrency: int = 1

    def as_json(self) -> dict[str, Any]:
        return {
            "verify_enabled": self.verify_enabled,
            "verify_hash_on_disk": self.verify_hash_on_disk,
            "extract_enabled": self.extract_enabled,
            "extract_target_dir": self.extract_target_dir,
            "extract_passwords": list(self.extract_passwords),
            "failed_retention_enabled": self.failed_retention_enabled,
            "failed_retention_days": self.failed_retention_days,
            "delete_archives_after_extract": self.delete_archives_after_extract,
            "move_enabled": self.move_enabled,
            "concurrency": self.concurrency,
        }


async def load_postprocess_settings(db: aiosqlite.Connection) -> PostprocessSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return PostprocessSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return PostprocessSettings()
    passwords = data.get("extract_passwords") or []
    return PostprocessSettings(
        verify_enabled=bool(data.get("verify_enabled", False)),
        verify_hash_on_disk=bool(data.get("verify_hash_on_disk", False)),
        extract_enabled=bool(data.get("extract_enabled", False)),
        extract_target_dir=data.get("extract_target_dir"),
        extract_passwords=tuple(passwords),
        failed_retention_enabled=bool(data.get("failed_retention_enabled", False)),
        failed_retention_days=float(
            data.get("failed_retention_days", extract.FAILED_RETENTION_DEFAULT_DAYS)
        ),
        delete_archives_after_extract=bool(data.get("delete_archives_after_extract", False)),
        move_enabled=bool(data.get("move_enabled", False)),
        concurrency=max(1, int(data.get("concurrency", 1))),
    )


async def save_postprocess_settings(
    db: aiosqlite.Connection, settings: PostprocessSettings
) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps(settings.as_json())),
    )
    await db.commit()


# --- The staging -> final move (DESIGN.md §6) ------------------------------------------------


def move_tree(src: Path, dst: Path, *, merge: bool = False) -> None:
    """Relocate `src` (file or directory) to `dst`, atomically from an observer's point of
    view: `dst` never exists in a partially-written state.

    Fast path: `os.rename` -- atomic, same-filesystem only. Cross-device (`errno.EXDEV`,
    the expected case for this project's deployment: the user's downloads live on NFS, not
    the exception -- DESIGN.md §6) falls back to copy-then-atomic-rename. The copy target is
    a hidden sibling of `dst` (same directory, hence guaranteed to share `dst`'s filesystem),
    so the *final* rename is always same-device and therefore atomic; only after every byte
    has been written and fsynced does that rename happen. If the copy fails partway, the
    sibling is removed and `dst` is never created -- a partial copy must never be mistaken
    for a complete one.

    `merge=False` (default) refuses to touch an existing `dst` at all -- `FileExistsError`.
    `merge=True` (used by `core/extract.py`'s `_UNPACK_` staging, DESIGN.md §6: extraction's
    final directory routinely already exists and already holds the source archives) instead
    walks `src` and `dst` together: a directory that exists on both sides merges recursively;
    anything else -- a file colliding with an existing file, or a file/directory type
    mismatch -- still raises `FileExistsError` rather than silently overwriting content that
    arrived by another route. Each leaf move reuses this same function (non-merging), so the
    EXDEV fallback above applies at every level, not just the top.
    """
    if merge and dst.is_dir() and src.is_dir():
        _merge_tree(src, dst)
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"move target already exists: {dst}")

    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    tmp = dst.parent / f".lftpweb-moving-{uuid4().hex}"
    try:
        _copy_tree_fsync(src, tmp)
        os.rename(tmp, dst)  # same filesystem as dst by construction -> atomic
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(src, ignore_errors=True)
    _fsync_dir(dst.parent)


def _merge_tree(src: Path, dst: Path) -> None:
    """The `merge=True` walk: `src` and `dst` are both directories that already exist. A
    same-named child directory on both sides recurses; anything else is handed to
    `move_tree` un-merged, which moves it wholesale if `dst` has no same-named entry yet, or
    raises `FileExistsError` if it does -- a file colliding with existing content is a
    conflict to surface, not a byte-for-byte-identical assumption to act on silently.
    """
    for entry in sorted(src.iterdir()):
        dst_entry = dst / entry.name
        if entry.is_dir() and dst_entry.is_dir():
            _merge_tree(entry, dst_entry)
        else:
            move_tree(entry, dst_entry)
    src.rmdir()  # every child has been moved out (or the loop already raised)


def _copy_tree_fsync(src: Path, dst: Path) -> None:
    if src.is_dir():
        dst.mkdir(parents=True)
        for entry in sorted(src.iterdir()):
            _copy_tree_fsync(entry, dst / entry.name)
        _fsync_dir(dst)
    else:
        with src.open("rb") as rf, open(dst, "wb") as wf:
            shutil.copyfileobj(rf, wf)
            wf.flush()
            os.fsync(wf.fileno())
        shutil.copystat(src, dst)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # best-effort -- not every platform/filesystem supports fsync on a directory fd


# --- The pipeline itself ----------------------------------------------------------------------


class _RemotePool(Protocol):
    async def delete_path(self, host: HostConfig, remote_path: str) -> None: ...


class PostprocessPipeline:
    """Owns scheduling (one asyncio task per triggered item, bounded by
    `PostprocessSettings.concurrency`) and the verify/delete/extract/move sequence for one
    item. One instance lives on `app.state.postprocess` for the process lifetime, alongside
    `core/engine.py`'s `Engine` and `core/queue.py`'s `TransferQueue` (DESIGN.md §2).
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        events: EventBus,
        remote_pool: _RemotePool,
        *,
        host_provider: Any = None,
    ) -> None:
        self.db = db
        self.events = events
        self.remote_pool = remote_pool
        # Callable[[], Awaitable[HostConfig | None]] -- the identical seam core/queue.py's
        # TransferQueue uses, so credential decryption still has exactly one implementation.
        self._host_provider = host_provider

        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0
        self._tasks: set[asyncio.Task] = set()
        # item id -> how many workers are inside `process_item` for it right now -- see
        # `in_flight_item_ids`. A count rather than a set because two triggers for the same
        # item can overlap when `concurrency > 1`, and the first one to finish must not
        # un-protect an item the second is still working on.
        self._in_flight: dict[int, int] = {}

    def trigger(self, item_id: int) -> None:
        """Fire-and-forget: schedule `process_item` on its own task. Called from
        `core/queue.py._reap_one`, which must not itself block on postprocessing.
        """
        task = asyncio.create_task(
            self._run_guarded(item_id), name=f"lftpweb-postprocess-item-{item_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def in_flight_item_ids(self) -> frozenset[int]:
        """Items a worker is running for at this instant -- read by `core/engine.py`'s scan
        pass, which leaves their `state` alone (`Engine._protected_rel_paths`) so a 30s scan
        can't stomp `VERIFYING`/`EXTRACTING` out from under a job that is genuinely still
        going, however long it takes.

        **This is deliberately in-memory, and that is the whole recovery mechanism.** A
        transient state is protected by the worker's own existence, never by the state string
        itself, so there is no way to wedge an item on one: kill the process mid-extract and
        the set comes back empty, the next scan recomputes the item structurally, and it
        reads `DOWNLOADED`/`PARTIAL` again within one scan interval. Same for a worker that
        dies by exception -- `process_item`'s `finally` clears the entry whatever happens.
        Phase 3 fixed exactly this bug shape for jobs left `running` by a restart
        (`core/queue.py._reconcile_orphaned_jobs`) and needed a startup sweep to do it,
        because `job.state` is durable; nothing durable is written here, so nothing durable
        needs sweeping.
        """
        return frozenset(self._in_flight)

    async def wait_idle(self) -> None:
        """Test/shutdown helper: wait for every currently-scheduled item to finish."""
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_guarded(self, item_id: int) -> None:
        settings = await load_postprocess_settings(self.db)
        sem = self._semaphore(settings.concurrency)
        async with sem:
            try:
                await self.process_item(item_id, settings)
            except Exception:  # noqa: BLE001 - one bad item must not break the pipeline
                logger.exception("postprocessing failed for item %s", item_id)

    def _semaphore(self, concurrency: int) -> asyncio.Semaphore:
        if self._sem is None or self._sem_size != concurrency:
            self._sem = asyncio.Semaphore(max(1, concurrency))
            self._sem_size = concurrency
        return self._sem

    async def process_item(self, item_id: int, settings: PostprocessSettings | None = None) -> None:
        """Run the pipeline for one item, holding it in `in_flight_item_ids()` for exactly as
        long as that takes -- `finally`, so an exception (or a cancellation on shutdown)
        releases the item rather than leaving it protected with nobody working on it.
        """
        self._in_flight[item_id] = self._in_flight.get(item_id, 0) + 1
        try:
            await self._process_item(item_id, settings)
        finally:
            remaining = self._in_flight.get(item_id, 1) - 1
            if remaining > 0:
                self._in_flight[item_id] = remaining
            else:
                self._in_flight.pop(item_id, None)

    async def _process_item(
        self, item_id: int, settings: PostprocessSettings | None = None
    ) -> None:
        settings = settings if settings is not None else await load_postprocess_settings(self.db)
        item = await self._fetch_item(item_id)
        if item is None or item["state"] != "DOWNLOADED":
            # Stale trigger: the item moved on (stopped, retried, rescanned) since it was
            # scheduled. Nothing to do -- never act on a state this module didn't observe.
            return
        queue = await self._fetch_queue(item["queue_id"])
        if queue is None:
            return

        if not item["rel_path"]:
            logger.warning("postprocess: item %s has an empty rel_path, refusing to act", item_id)
            return

        local_root = Path(queue["local_path"].rstrip("/")) / item["rel_path"]
        sync_mode = queue["sync_mode"]

        # DESIGN.md §6: verification is forced on for `move` regardless of either toggle --
        # see the module docstring and docs/decisions.md.
        verify_effective = (
            settings.verify_enabled and bool(queue["auto_verify"])
        ) or sync_mode == "move"
        extract_effective = settings.extract_enabled and bool(queue["auto_extract"])
        move_effective = (
            settings.move_enabled and bool(queue["auto_move"]) and bool(queue["staging_path"])
        )

        verify_state: str | None = None
        if verify_effective:
            verify_state = await self._do_verify(item, local_root, settings)

        if sync_mode == "move":
            await self._maybe_delete_remote(item, queue, verify_state)

        if extract_effective:
            await self._do_extract(item, queue, local_root, settings)

        if move_effective:
            await self._do_move(item, queue, local_root)

    # --- steps ---------------------------------------------------------------------------

    async def _do_verify(self, item: Any, local_root: Path, settings: PostprocessSettings) -> str:
        await self._set_item_state(item["id"], "VERIFYING")
        result = await asyncio.to_thread(
            verify.verify_item,
            local_root,
            hash_on_disk_fallback=settings.verify_hash_on_disk,
            # prompts/open-issues.md #3: without this, the hash-on-disk fallback proves a file
            # is readable, not that it's complete -- a truncated file reads to EOF cleanly.
            # `item["remote_size"]` is the same total `core/reconcile.py` rolls up for
            # completeness elsewhere; `None` if a scan never populated it, which disables the
            # extra check rather than failing verification on missing information.
            expected_total_bytes=item["remote_size"],
        )
        if result.state == "VERIFIED":
            await self.db.execute(
                "UPDATE item SET state = 'VERIFIED', verified_at = ?, "
                "error_class = NULL, error_detail = NULL WHERE id = ?",
                (_now_iso(), item["id"]),
            )
        elif result.state == "CORRUPT":
            await self.db.execute(
                "UPDATE item SET state = 'CORRUPT', error_class = 'CORRUPT', error_detail = ? WHERE id = ?",
                (result.detail[:2000], item["id"]),
            )
        else:  # SKIPPED -- no evidence either way; don't strand the item on VERIFYING
            await self.db.execute(
                "UPDATE item SET state = 'DOWNLOADED' WHERE id = ?", (item["id"],)
            )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info" if result.state == "VERIFIED" else "warning",
            item_id=item["id"],
            kind="verify",
            message=f"{result.state}: {result.detail}",
        )
        await self._publish(item["id"])
        return result.state

    async def _maybe_delete_remote(self, item: Any, queue: Any, verify_state: str | None) -> None:
        """The `move`-mode delete gate (DESIGN.md §7.3/§7.4). Every branch -- delete or
        withhold -- writes an `event` row before returning; there is no silent path here.
        """
        item_id = item["id"]
        queue_id = queue["id"]

        if verify_state != "VERIFIED":
            reason = (
                "verification produced no usable result for this move-mode item "
                "(no .sfv/.md5 evidence and hash-on-disk verification is disabled)"
                if verify_state in (None, "SKIPPED")
                else f"verification result was {verify_state}, not VERIFIED"
            )
            await audit.record_event(
                self.db,
                level="warning",
                item_id=item_id,
                kind="remote_delete_withheld",
                message=f"queue {queue_id} ('{queue['name']}') mode=move: delete withheld -- {reason}",
            )
            await self._publish(item_id)
            return

        host = await self._host_provider() if self._host_provider else None
        if host is None:
            await audit.record_event(
                self.db,
                level="error",
                item_id=item_id,
                kind="remote_delete_withheld",
                message=f"queue {queue_id} ('{queue['name']}') mode=move: delete withheld -- no host configured",
            )
            await self._publish(item_id)
            return

        remote_full = queue["remote_path"].rstrip("/") + "/" + item["rel_path"]
        try:
            await self.remote_pool.delete_path(host, remote_full)
        except Exception as exc:  # noqa: BLE001 - always recorded, never re-raised
            logger.exception("move-mode delete failed for item %s (%s)", item_id, remote_full)
            await audit.record_event(
                self.db,
                level="error",
                item_id=item_id,
                kind="remote_delete_failed",
                message=f"queue {queue_id} ('{queue['name']}') mode=move: delete of {remote_full} failed: {exc}",
            )
            await self._publish(item_id)
            return

        await self.db.execute(
            "UPDATE item SET remote_deleted_at = ? WHERE id = ?", (_now_iso(), item_id)
        )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info",
            item_id=item_id,
            kind="remote_delete",
            message=f"queue {queue_id} ('{queue['name']}') mode=move: deleted verified remote copy {remote_full}",
        )
        await self._publish(item_id)

    async def _do_extract(
        self, item: Any, queue: Any, local_root: Path, settings: PostprocessSettings
    ) -> None:
        """Fix, 2026-08-12 (docs/decisions.md): `ok: bool` used to conflate "nothing to
        extract" with "extraction succeeded" -- a plain `.mkv` download on an auto-extract
        queue got stamped `EXTRACTED`, with a real `extracted_at`, for work that never
        happened. The pre-check below (`find_archives`, not `extract_item`) is what stops
        that: when there is nothing to extract, this method returns having never called
        `_set_item_state(..., "EXTRACTING")` at all -- no transient-state flicker on the Files
        page for every non-archive item, which is most of them, and no state change to
        restore afterwards. The capture-and-restore branch inside the try below exists only
        for a no-op `extract_item` itself discovers *late* (a race between this pre-check and
        the real call) -- see `core/extract.py.extract_item`'s docstring for why that path
        must restore the item's actual prior state, never hardcode `DOWNLOADED`.
        """
        if settings.failed_retention_enabled:
            await self._sweep_failed_dirs(queue, settings)

        archives = await asyncio.to_thread(extract.find_archives, local_root)
        if not archives:
            await audit.record_event(
                self.db,
                level="info",
                item_id=item["id"],
                kind="extract",
                message=extract.NO_ARCHIVES_DETAIL,
            )
            await self._publish(item["id"])
            return

        # Read fresh, not from the `item` row `_process_item` fetched at the top of this run:
        # verification (if it ran first this pass) may already have moved the item off
        # DOWNLOADED, and that in-memory row would still say DOWNLOADED regardless.
        current = await self._fetch_item(item["id"])
        prior_state = current["state"] if current is not None else item["state"]

        await self._set_item_state(item["id"], "EXTRACTING")
        target = (
            Path(settings.extract_target_dir) / item["rel_path"]
            if settings.extract_target_dir
            else None
        )
        result = await asyncio.to_thread(
            extract.extract_item,
            local_root,
            target_dir=target,
            passwords=settings.extract_passwords,
        )
        if result.state == "EXTRACTED":
            await self.db.execute(
                "UPDATE item SET state = 'EXTRACTED', extracted_at = ? WHERE id = ?",
                (_now_iso(), item["id"]),
            )
        elif result.state == "EXTRACT_FAILED":
            await self.db.execute(
                "UPDATE item SET state = 'EXTRACT_FAILED', error_class = 'EXTRACT_FAILED', "
                "error_detail = ? WHERE id = ?",
                (result.detail[:2000], item["id"]),
            )
        else:  # SKIPPED, discovered late -- restore exactly what this run found, never a
            # hardcoded DOWNLOADED (see this method's own docstring).
            await self.db.execute(
                "UPDATE item SET state = ? WHERE id = ?", (prior_state, item["id"])
            )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info" if result.state in ("EXTRACTED", "SKIPPED") else "warning",
            item_id=item["id"],
            kind="extract",
            message=result.detail,
        )
        await self._publish(item["id"])

        # 2026-08-13 (prompts/2026-08-13-delete-archives-after-extract.md): only on a *full*
        # success -- never EXTRACT_FAILED, never a precondition failure (which also reports as
        # EXTRACT_FAILED, caught by this same check), never SKIPPED (nothing was extracted, so
        # `archives` is never even reached; the guard above already returned). `archives` is
        # `find_archives`'s pre-extraction listing (first volume of each set only) -- untouched
        # by `extract_item`, which only ever writes to its own staging directory and merges into
        # `local_root`, so it is still exactly what to expand and remove.
        if result.state == "EXTRACTED" and settings.delete_archives_after_extract:
            # Imported locally: `core/local_delete.py` imports `core/mount_sentinel.py`, which
            # imports this module for `OWNED_STATES` -- a top-level import here would be
            # circular (the same reason `core/extract.py.extract_item` imports `move_tree`
            # locally).
            from lftpweb.core import local_delete

            await local_delete.delete_extracted_archives(
                self.db, item=item, queue=queue, archive_heads=archives
            )

    async def _sweep_failed_dirs(self, queue: Any, settings: PostprocessSettings) -> None:
        """Fix, 2026-08-12 (docs/decisions.md): `_FAILED_` staging directories
        (`core/extract.py`) were kept as diagnostic evidence forever -- correct on failure,
        but nothing ever removed them, and `core/local_scan.py` filters the prefix out of
        every scan, so they consumed disk invisibly. Runs on the same pass that would create a
        new one (this method's only caller, `_do_extract`), gated by
        `PostprocessSettings.failed_retention_enabled` -- default off, this project's rule for
        anything that deletes, regardless of how conservative the containment check
        (`extract.sweep_failed_dirs`) already is.
        """
        removed = await asyncio.to_thread(
            extract.sweep_failed_dirs,
            Path(queue["local_path"].rstrip("/")),
            max_age_days=settings.failed_retention_days,
        )
        for path, age_days in removed:
            item_id = await self._find_item_id_for_failed_dir(queue["id"], path)
            await audit.record_event(
                self.db,
                level="info",
                item_id=item_id,
                kind="failed_dir_removed",
                message=(
                    f"removed stale extraction-failure evidence {path} "
                    f"(age {age_days:.1f}d >= retention {settings.failed_retention_days}d)"
                ),
            )
            if item_id is not None:
                await self._publish(item_id)

    async def _find_item_id_for_failed_dir(self, queue_id: int, path: Path) -> int | None:
        """Best-effort: `_FAILED_<name>` directories are not tied to a live item row by
        construction (the item that produced one may since have been re-downloaded, or the
        directory may outlive it entirely), but its `rel_path` is recoverable from its own
        name -- so the removal event can usually still be found from that item's own audit
        trail, matching DESIGN.md §6's "something invisible that consumes disk is worse than
        something ugly" for the *removal* record, not only the original failure. `None` if no
        matching item exists any more; the event row still stands on its own, with the path in
        the message.
        """
        rel_path = path.name[len(extract.FAILED_PREFIX) :]
        cursor = await self.db.execute(
            "SELECT id FROM item WHERE queue_id = ? AND rel_path = ?", (queue_id, rel_path)
        )
        row = await cursor.fetchone()
        return row["id"] if row is not None else None

    async def _do_move(self, item: Any, queue: Any, local_root: Path) -> None:
        dest = Path(queue["staging_path"].rstrip("/")) / item["rel_path"]
        try:
            await asyncio.to_thread(move_tree, local_root, dest)
        except Exception as exc:  # noqa: BLE001 - always recorded, pipeline continues
            logger.exception("staging move failed for item %s", item["id"])
            await audit.record_event(
                self.db,
                level="error",
                item_id=item["id"],
                kind="move_failed",
                message=f"relocating {local_root} -> {dest} failed: {exc}",
            )
            return
        await audit.record_event(
            self.db,
            level="info",
            item_id=item["id"],
            kind="move",
            message=f"relocated {local_root} -> {dest}",
        )
        # No item.state change here -- see the module docstring: the next scan finds
        # local_path empty for this rel_path and phase 4's REMOVED_LOCAL grace-period
        # machinery takes it from there, the same as any other externally-caused move.

    # --- small DB helpers ------------------------------------------------------------------

    async def _fetch_item(self, item_id: int):
        cursor = await self.db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        return await cursor.fetchone()

    async def _fetch_queue(self, queue_id: int):
        cursor = await self.db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
        return await cursor.fetchone()

    async def _set_item_state(self, item_id: int, state: str) -> None:
        await self.db.execute("UPDATE item SET state = ? WHERE id = ?", (state, item_id))
        await self.db.commit()
        await self._publish(item_id)

    async def _publish(self, item_id: int) -> None:
        row = await self._fetch_item(item_id)
        if row is None:
            return
        self.events.publish(
            {
                "type": "item_delta",
                "queue_id": row["queue_id"],
                "nodes": [item_view(row)],
            }
        )
