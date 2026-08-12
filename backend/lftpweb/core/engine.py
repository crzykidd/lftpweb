"""The asyncio loop that owns scanning and holds the current model (DESIGN.md §2).

Phase 2 scope: no job queue, no scheduler, no lftp process — this is scanning and
reconciliation only. Every `scan_interval_s` (default 30s, DESIGN.md §5), and on demand via
`request_rescan()`, the engine re-loads `host` + `path_queue` from the database (so a config
change takes effect on the *next* cycle without a restart), scans each enabled queue's remote
and local trees, reconciles them (`core/reconcile.py`), persists the result to `item` rows, and
publishes the fresh model over `core/events.py` for `api/ws.py` to fan out.

**Simplification recorded rather than silently taken:** DESIGN.md §5 specifies two cadences —
remote scan every 30s, a faster local-only walk every 10s (the 1 Hz active-file poll that
covers the gap is phase 3's `ProgressSampler`, since there's no active transfer to sample yet).
Phase 2 runs one combined interval instead; see docs/decisions.md. Splitting the cadence is a
scale optimization for when scans are expensive, not a phase 2 correctness requirement — every
verification in the phase 2 prompt is satisfied by a fresh combined scan, and `request_rescan()`
gives an immediate on-demand path that doesn't wait on either interval.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import local_scan
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.reconcile import ReconciledNode, reconcile
from lftpweb.core.remote import HostConfig, RemoteConnectionPool, RemoteScanError

logger = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_S = 30.0


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


async def load_host_config(db: aiosqlite.Connection, config_dir: str) -> HostConfig | None:
    """Load the (single, v1) host row and decrypt its password if applicable.

    Returns `None` if no host is configured yet. Decryption failure (DESIGN.md §8's
    "credentials need re-entry") is *not* raised here — it's deferred until something
    actually needs the password (`core/remote.py` raises `DecryptionNeededError` at connect
    time), so a key-auth or agent-auth host isn't blocked by an unrelated password field.
    """
    cursor = await db.execute(
        "SELECT id, address, port, username, auth_method, key_path, password_enc, "
        "known_hosts_policy FROM host ORDER BY id LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    password: str | None = None
    if row["auth_method"] == "password" and row["password_enc"]:
        try:
            password = decrypt_secret(config_dir, row["password_enc"])
        except DecryptionError as exc:
            logger.warning(
                "host %s: password does not decrypt (credentials need re-entry): %s", row["id"], exc
            )
            password = None

    return HostConfig(
        id=row["id"],
        address=row["address"],
        port=row["port"],
        username=row["username"],
        auth_method=row["auth_method"],
        key_path=row["key_path"],
        password=password,
        known_hosts_policy=row["known_hosts_policy"],
    )


async def load_queues(db: aiosqlite.Connection) -> list[QueueConfig]:
    cursor = await db.execute(
        "SELECT id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode "
        "FROM path_queue ORDER BY id"
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
        )
        for row in rows
    ]


def serialize_node(node: ReconciledNode) -> dict[str, Any]:
    from lftpweb.core.util import to_safe_text

    return {
        "rel_path": to_safe_text(node.rel_path),
        "is_dir": node.is_dir,
        "state": node.state,
        "remote_size": node.remote_size,
        "local_size": node.local_size,
        "remote_mtime": node.remote_mtime,
    }


def diff_nodes(
    old: dict[str, ReconciledNode], new: dict[str, ReconciledNode]
) -> tuple[list[ReconciledNode], list[str]]:
    """The WebSocket delta fix (DESIGN.md §2/§9; docs/decisions.md's phase 2 entry flagged
    this shape as scoped-down and said phase 3 shouldn't inherit it by default).

    Phase 2 published one full-tree `queue_snapshot` — every node, every scan — because
    nothing else existed yet and a 30s cadence made it cheap. Phase 3a's ~1 Hz progress
    sampler makes that shape actively wrong: a queue holding a few thousand files would
    re-serialize and re-send the entire tree to every connected browser every second.

    `ReconciledNode` is a frozen, `eq`-comparable dataclass (`core/reconcile.py`), so
    "changed" is exactly the identity check below — no field-by-field bookkeeping needed. A
    node whose `rel_path` didn't exist in `old`, or whose value differs from `old`'s, is
    "changed"; a `rel_path` present in `old` but absent from `new` is "removed". This is a
    pure function precisely so it's unit-testable without a running engine or a live SSH
    connection — see `tests/test_ws_deltas.py`, which is also where "the payload doesn't
    scale with tree size" is proven, not just asserted.
    """
    changed = [node for rel_path, node in new.items() if old.get(rel_path) != node]
    removed = [rel_path for rel_path in old if rel_path not in new]
    return changed, removed


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
    ) -> None:
        self.db = db
        self.config_dir = config_dir
        self.events = events
        self.scan_interval_s = scan_interval_s
        self.pool = RemoteConnectionPool(Path(config_dir))

        self.models: dict[int, dict[str, ReconciledNode]] = {}
        self.queue_meta: dict[int, QueueConfig] = {}
        self.scan_errors: dict[int, str | None] = {}
        # A *soft* per-queue note (DESIGN.md §5) — set when a scan completed but skipped one
        # or more unreadable remote subtrees (core/remote.py's scan-abort fix) rather than
        # failing outright. Distinct from `scan_errors`, which means the whole scan failed.
        self.scan_warnings: dict[int, str | None] = {}
        self.last_scan_at: dict[int, str | None] = {}

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
        """Trigger an immediate scan pass rather than waiting for the interval — used by the
        on-demand rescan API and by *Test connection* succeeding after a config change.
        """
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.scan_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("engine scan cycle failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.scan_interval_s)
            except TimeoutError:
                pass
            self._wake.clear()

    async def scan_all(self) -> None:
        host = await load_host_config(self.db, self.config_dir)
        queues = await load_queues(self.db)
        self.queue_meta = {q.id: q for q in queues}
        for q in queues:
            if not q.enabled:
                continue
            await self.scan_queue(q, host)

    async def scan_queue(self, q: QueueConfig, host: HostConfig | None) -> None:
        try:
            if host is None:
                raise RemoteScanError("no host configured")
            remote_tree, scan_warning = await self.pool.scan(host, q.remote_path)
            local_tree = local_scan.scan_local(q.local_path)
            nodes = reconcile(remote_tree, local_tree)

            old_nodes = self.models.get(q.id, {})
            changed, removed = diff_nodes(old_nodes, nodes)

            self.models[q.id] = nodes
            await self._persist(q.id, nodes)
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
                    "changed": [serialize_node(n) for n in changed],
                    "removed": removed,
                    "scanned_at": self.last_scan_at[q.id],
                    "warning": scan_warning,
                }
            )
        except Exception as exc:  # noqa: BLE001 - recorded per-queue, never propagated
            message = str(exc)
            self.scan_errors[q.id] = message
            logger.warning("scan failed for queue %s (%s): %s", q.id, q.name, message)
            self.events.publish(
                {"type": "scan_error", "queue_id": q.id, "queue_name": q.name, "message": message}
            )

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
        """
        cursor = await self.db.execute(
            "SELECT item.rel_path FROM item WHERE item.queue_id = ? AND ("
            "  item.auto_queue_suppressed = 1"
            "  OR EXISTS (SELECT 1 FROM job WHERE job.item_id = item.id AND job.state IN ('queued', 'running'))"
            ")",
            (queue_id,),
        )
        rows = await cursor.fetchall()
        return {row["rel_path"] for row in rows}

    async def _persist(self, queue_id: int, nodes: dict[str, ReconciledNode]) -> None:
        from lftpweb.core.util import to_safe_text

        protected = await self._protected_rel_paths(queue_id)

        for node in nodes.values():
            rel_path = to_safe_text(node.rel_path)
            if rel_path in protected:
                await self.db.execute(
                    """
                    INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                        is_dir = excluded.is_dir,
                        remote_size = excluded.remote_size,
                        local_size = excluded.local_size,
                        remote_mtime = excluded.remote_mtime
                    """,
                    (
                        queue_id,
                        rel_path,
                        1 if node.is_dir else 0,
                        node.remote_size,
                        node.local_size,
                        node.remote_mtime,
                        node.state,
                    ),
                )
            else:
                await self.db.execute(
                    """
                    INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                        is_dir = excluded.is_dir,
                        remote_size = excluded.remote_size,
                        local_size = excluded.local_size,
                        remote_mtime = excluded.remote_mtime,
                        state = excluded.state
                    """,
                    (
                        queue_id,
                        rel_path,
                        1 if node.is_dir else 0,
                        node.remote_size,
                        node.local_size,
                        node.remote_mtime,
                        node.state,
                    ),
                )
        await self.db.commit()

    def snapshot(self) -> list[dict[str, Any]]:
        """The full current model, one message per queue — what a freshly-connected
        WebSocket client gets before any delta (DESIGN.md §2, §9). This is the *only* place
        a full node list is ever sent; every update after connect is a `queue_delta`
        (`scan_queue`) or an `item_delta` (`core/queue.py`), each proportional to what
        changed rather than to the size of the tree.
        """
        out = []
        for queue_id, nodes in self.models.items():
            meta = self.queue_meta.get(queue_id)
            out.append(
                {
                    "type": "queue_snapshot",
                    "queue_id": queue_id,
                    "queue_name": meta.name if meta else "",
                    "nodes": [serialize_node(n) for n in nodes.values()],
                    "scanned_at": self.last_scan_at.get(queue_id),
                    "warning": self.scan_warnings.get(queue_id),
                }
            )
        return out
