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
            logger.warning("host %s: password does not decrypt (credentials need re-entry): %s", row["id"], exc)
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
            remote_tree = await self.pool.scan(host, q.remote_path)
            local_tree = local_scan.scan_local(q.local_path)
            nodes = reconcile(remote_tree, local_tree)

            self.models[q.id] = nodes
            await self._persist(q.id, nodes)
            self.scan_errors[q.id] = None
            self.last_scan_at[q.id] = _now_iso()
            self.events.publish(
                {
                    "type": "queue_snapshot",
                    "queue_id": q.id,
                    "queue_name": q.name,
                    "nodes": [serialize_node(n) for n in nodes.values()],
                    "scanned_at": self.last_scan_at[q.id],
                }
            )
        except Exception as exc:  # noqa: BLE001 - recorded per-queue, never propagated
            message = str(exc)
            self.scan_errors[q.id] = message
            logger.warning("scan failed for queue %s (%s): %s", q.id, q.name, message)
            self.events.publish({"type": "scan_error", "queue_id": q.id, "queue_name": q.name, "message": message})

    async def _persist(self, queue_id: int, nodes: dict[str, ReconciledNode]) -> None:
        from lftpweb.core.util import to_safe_text

        for node in nodes.values():
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
                    to_safe_text(node.rel_path),
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
        WebSocket client gets before any delta (DESIGN.md §2, §9).
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
                }
            )
        return out
