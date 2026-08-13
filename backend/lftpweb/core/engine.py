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

from lftpweb.core import local_scan, mount_sentinel, patterns, postprocess
from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import ITEM_VIEW_COLUMNS, ItemView, item_view
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
    # Phase 4 (DESIGN.md §4.7): per-queue, default off (migration 002 adds the column with
    # DEFAULT 0; migration 001 already defaulted auto_queue_enabled to 0). A capability that
    # turns itself on for an existing queue is a bug -- see docs/decisions.md.
    auto_queue_enabled: bool = False
    auto_queue_patterns_only: bool = False


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

    return HostConfig(
        id=row["id"],
        address=row["address"],
        port=row["port"],
        username=row["username"],
        auth_method=row["auth_method"],
        key_path=row["key_path"],
        password=password,
        known_hosts_policy=row["known_hosts_policy"],
        credentials_need_reentry=credentials_need_reentry,
    )


async def load_queues(db: aiosqlite.Connection) -> list[QueueConfig]:
    cursor = await db.execute(
        "SELECT id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode, "
        "auto_queue_enabled, auto_queue_patterns_only FROM path_queue ORDER BY id"
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
            local_tree = local_scan.scan_local(q.local_path)

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
            counts_predicate = patterns.build_counts_predicate(compiled)
            nodes = reconcile(remote_tree, local_tree, counts_predicate=counts_predicate)

            # Persist first, then read back what was actually stored, then diff *that*
            # (DESIGN.md §2/§9; `core/itemview.py`). `_persist` is where an item's state is
            # really decided — job-lifecycle protection, §6's post-processing precedence,
            # §7.3's grace period — so diffing `nodes` here, as this did until the two views
            # were reconciled, published a state the database disagreed with. The order is
            # the invariant: nothing goes on the wire that wasn't read back out of `item`.
            written = await self._persist(q.id, nodes)
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
        """
        # `frozenset` -> sorted list purely so the SQL parameters are deterministic (log/test
        # readability); membership is what matters, not order.
        in_flight = sorted(self.postprocess.in_flight_item_ids()) if self.postprocess else []
        in_flight_clause = (
            f" OR item.id IN ({','.join('?' for _ in in_flight)})" if in_flight else ""
        )
        cursor = await self.db.execute(
            "SELECT item.rel_path FROM item WHERE item.queue_id = ? AND ("
            "  item.auto_queue_suppressed = 1"
            "  OR EXISTS (SELECT 1 FROM job WHERE job.item_id = item.id AND job.state IN ('queued', 'running'))"
            f"{in_flight_clause}"
            ")",
            (queue_id, *in_flight),
        )
        rows = await cursor.fetchall()
        return {row["rel_path"] for row in rows}

    async def _previous_states(self, queue_id: int) -> dict[str, tuple[str, str | None]]:
        """`rel_path -> (state, first_missing_at)` as currently persisted, for the grace-period
        decision below. One query per scan, same shape as `_protected_rel_paths`.
        """
        cursor = await self.db.execute(
            "SELECT rel_path, state, first_missing_at FROM item WHERE queue_id = ?", (queue_id,)
        )
        rows = await cursor.fetchall()
        return {row["rel_path"]: (row["state"], row["first_missing_at"]) for row in rows}

    async def _persist(self, queue_id: int, nodes: dict[str, ReconciledNode]) -> set[str]:
        """Write this pass's arbitrated state for every reconciled node, and return the
        `rel_path`s it wrote (already `to_safe_text`-ed, i.e. keyed exactly as the `item`
        table stores them) so `_project` knows which rows this scan is entitled to publish.
        """
        from lftpweb.core.util import to_safe_text

        protected = await self._protected_rel_paths(queue_id)
        previous = await self._previous_states(queue_id)
        mount_ok = self.mount_ok.get(queue_id, False)
        now = datetime.now(UTC)
        written: set[str] = set()

        for node in nodes.values():
            rel_path = to_safe_text(node.rel_path)
            written.add(rel_path)
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
                        node.structural_state,
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
            prev_state, prev_first_missing_at = previous.get(rel_path, (None, None))
            if postprocess.outcome_survives_rescan(prev_state, node.structural_state):
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

            await self.db.execute(
                """
                INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, first_missing_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                    is_dir = excluded.is_dir,
                    remote_size = excluded.remote_size,
                    local_size = excluded.local_size,
                    remote_mtime = excluded.remote_mtime,
                    state = excluded.state,
                    first_missing_at = excluded.first_missing_at
                """,
                (
                    queue_id,
                    rel_path,
                    1 if node.is_dir else 0,
                    node.remote_size,
                    node.local_size,
                    node.remote_mtime,
                    state,
                    first_missing_at,
                ),
            )
        await self.db.commit()
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
        """
        cursor = await self.db.execute(
            f"SELECT {ITEM_VIEW_COLUMNS} FROM item WHERE queue_id = ?",  # noqa: S608 - a module constant, not user input
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
