"""Post-processing pipeline: verify, extract, staging move (DESIGN.md §6) -- plus the
`move`-mode remote delete this phase adds on top of it (§7, §7.4).

**Trigger.** `core/queue.py._reap_one`'s job-success path calls `PostprocessPipeline.trigger`
the moment a *top-level* item (no `/` in `rel_path` -- the same eligibility shape
`core/autoqueue.py` uses) transitions to `DOWNLOADED`. Deliberately not also hooked into
`core/engine.py`'s scan-driven persistence: the only realistic way an item reaches
`DOWNLOADED` in this deployment is by lftpweb having just transferred it, and limiting the
trigger to the one code path that always fires keeps this phase from reaching back into
phase 2/3's already-verified scan/reconcile machinery. See `docs/decisions.md`.

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
from lftpweb.core.remote import HostConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def move_tree(src: Path, dst: Path) -> None:
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
    """
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

    def trigger(self, item_id: int) -> None:
        """Fire-and-forget: schedule `process_item` on its own task. Called from
        `core/queue.py._reap_one`, which must not itself block on postprocessing.
        """
        task = asyncio.create_task(
            self._run_guarded(item_id), name=f"lftpweb-postprocess-item-{item_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            await self._do_extract(item, local_root, settings)

        if move_effective:
            await self._do_move(item, queue, local_root)

    # --- steps ---------------------------------------------------------------------------

    async def _do_verify(self, item: Any, local_root: Path, settings: PostprocessSettings) -> str:
        await self._set_item_state(item["id"], "VERIFYING")
        result = await asyncio.to_thread(
            verify.verify_item, local_root, hash_on_disk_fallback=settings.verify_hash_on_disk
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

    async def _do_extract(self, item: Any, local_root: Path, settings: PostprocessSettings) -> None:
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
        if result.ok:
            await self.db.execute(
                "UPDATE item SET state = 'EXTRACTED', extracted_at = ? WHERE id = ?",
                (_now_iso(), item["id"]),
            )
        else:
            await self.db.execute(
                "UPDATE item SET state = 'EXTRACT_FAILED', error_class = 'EXTRACT_FAILED', "
                "error_detail = ? WHERE id = ?",
                (result.detail[:2000], item["id"]),
            )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info" if result.ok else "warning",
            item_id=item["id"],
            kind="extract",
            message=result.detail,
        )
        await self._publish(item["id"])

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
                "nodes": [
                    {
                        "id": row["id"],
                        "rel_path": row["rel_path"],
                        "is_dir": bool(row["is_dir"]),
                        "state": row["state"],
                        "remote_size": row["remote_size"],
                        "local_size": row["local_size"],
                        "remote_mtime": float(row["remote_mtime"])
                        if row["remote_mtime"] is not None
                        else None,
                    }
                ],
            }
        )
