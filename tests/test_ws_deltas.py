"""The WebSocket delta fix (DESIGN.md §2/§9; phase 3b's "do this first" requirement).

Phase 2 published one full-tree `queue_snapshot` on every scan — fine for a read-only tree
that changed every 30s. Phase 3a's ~1 Hz progress sampler makes that shape actively wrong: a
queue holding a few thousand files would re-serialize and re-send the entire tree to every
connected browser every second. `core/engine.py.diff_nodes` and `core/queue.py`'s
`_publish_item_state`/`_sample_and_publish_progress` are the fix — this file proves it with a
test, not by inspection: build a queue with many items, mutate a couple, and assert the
emitted payload contains only those and does not grow with total tree size.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

import lftpweb.core.engine as engine_module
from lftpweb.core.engine import Engine, QueueConfig, diff_nodes
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import ItemView, item_view
from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.mount_sentinel import DEFAULT_GRACE_S
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.db import migrate

# --- diff_nodes: the pure function, tested directly ----------------------------------------


def _node(rel_path: str, size: int) -> ItemView:
    """A projected `item` row, which is what `diff_nodes` compares now -- see
    `core/itemview.py`: nothing is published that wasn't read back out of the database, so the
    diff operates on the persisted projection rather than on `core/reconcile.py`'s structural
    node.
    """
    return item_view(
        {
            "id": 1,
            "rel_path": rel_path,
            "is_dir": 0,
            "state": "REMOTE_ONLY",
            "remote_size": size,
            "local_size": None,
            "remote_mtime": 1.0,
        }
    )


def _make_model(n: int) -> dict[str, ItemView]:
    return {f"file-{i:05d}.bin": _node(f"file-{i:05d}.bin", 1000 + i) for i in range(n)}


@pytest.mark.parametrize("n", [10, 1000, 20000])
def test_diff_nodes_reports_only_changed_and_removed_regardless_of_size(n):
    old = _make_model(n)
    new = dict(old)
    new["file-00001.bin"] = _node("file-00001.bin", 999_999)  # changed
    new["file-00002.bin"] = _node("file-00002.bin", 888_888)  # changed
    new["brand-new-file.bin"] = _node("brand-new-file.bin", 5)  # added
    del new["file-00003.bin"]  # removed

    changed, removed = diff_nodes(old, new)

    assert {n["rel_path"] for n in changed} == {
        "file-00001.bin",
        "file-00002.bin",
        "brand-new-file.bin",
    }
    assert removed == ["file-00003.bin"]


def test_diff_nodes_no_changes_yields_empty_delta():
    old = _make_model(500)
    new = dict(old)  # identical content, different dict object
    changed, removed = diff_nodes(old, new)
    assert changed == []
    assert removed == []


def test_diff_nodes_first_scan_reports_everything_as_changed():
    # There is no "old" on the very first scan (`self.models.get(q.id, {})` defaults to
    # empty) -- every node is legitimately new, so the delta *does* equal the full tree
    # exactly once, by construction, not because the fix regressed.
    new = _make_model(25)
    changed, removed = diff_nodes({}, new)
    assert len(changed) == 25
    assert removed == []


# --- Engine.scan_queue: the real code path, not just the pure helper -----------------------


def _release_tree(n: int) -> dict[str, RemoteEntry]:
    """A queue of `n` release directories, each with one file -- shaped like a realistic
    seedbox tree (directory + file, so a mutated file's rollup also changes its parent's
    size), not a flat file list.
    """
    tree: dict[str, RemoteEntry] = {}
    for i in range(n):
        d = f"Release.{i:05d}"
        f = f"{d}/movie.mkv"
        tree[d] = RemoteEntry(rel_path=d, is_dir=True, size=0, mtime=1.0)
        tree[f] = RemoteEntry(rel_path=f, is_dir=False, size=1_000_000 + i, mtime=1.0)
    return tree


class _FakePool:
    """Replaces `RemoteConnectionPool` for the duration of one test — hands back
    pre-built trees in order, exactly like a real scan would, without any SSH.
    """

    def __init__(self, trees: list[dict[str, RemoteEntry]]) -> None:
        self._trees = list(trees)

    async def scan(self, host, remote_path):  # noqa: ARG002 - matches RemoteConnectionPool.scan's signature
        return self._trees.pop(0), None


async def _make_engine(tmp_path, trees: list[dict[str, RemoteEntry]]):
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)

    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
        (host_id, str(tmp_path)),
    )
    await db.commit()
    queue_id = cursor.lastrowid

    engine = Engine(db, str(tmp_path), EventBus())
    engine.pool = _FakePool(trees)
    q = QueueConfig(
        id=queue_id,
        host_id=host_id,
        name="q",
        remote_path="/remote",
        local_path=str(tmp_path),
        staging_path=None,
        enabled=True,
        sync_mode="copy",
    )
    host = HostConfig(
        id=host_id,
        address="127.0.0.1",
        port=22,
        username="u",
        auth_method="password",
        password="x",
        known_hosts_policy="insecure",
    )
    return engine, q, host, db


async def _mutated_delta_payload(tmp_path, monkeypatch, n: int) -> tuple[dict, dict]:
    """Runs two scans of an `n`-item queue where only 2 files (and their parent dirs' rolled
    -up sizes) change and one release is removed. Returns `(delta_message, full_snapshot)` so
    a test can compare the two directly.
    """
    monkeypatch.setattr(engine_module.local_scan, "scan_local", lambda root: {})  # noqa: ARG005

    tree1 = _release_tree(n)
    tree2 = dict(tree1)
    tree2["Release.00001/movie.mkv"] = RemoteEntry(
        rel_path="Release.00001/movie.mkv", is_dir=False, size=9_999_999, mtime=2.0
    )
    tree2["Release.00002/movie.mkv"] = RemoteEntry(
        rel_path="Release.00002/movie.mkv", is_dir=False, size=8_888_888, mtime=2.0
    )
    del tree2["Release.00003"]
    del tree2["Release.00003/movie.mkv"]

    engine, q, host, db = await _make_engine(tmp_path, [tree1, tree2])
    try:
        subscription = engine.events.subscribe()

        await engine.scan_queue(q, host)  # baseline scan: populates engine.models
        await subscription.get()  # drain the first-scan delta (legitimately the whole tree)

        await engine.scan_queue(q, host)  # the mutation
        delta = await subscription.get()
        full_snapshot = (await engine.snapshot())[0]
        return delta, full_snapshot
    finally:
        await db.close()


@pytest.mark.parametrize("n", [20, 5000])
async def test_scan_delta_is_small_and_exact_regardless_of_tree_size(tmp_path, monkeypatch, n):
    delta, _ = await _mutated_delta_payload(tmp_path, monkeypatch, n)

    assert delta["type"] == "queue_delta"
    changed_paths = {node["rel_path"] for node in delta["changed"]}
    # 2 mutated files + their 2 parent directories (rollup size changed too) -- never more.
    assert changed_paths == {
        "Release.00001/movie.mkv",
        "Release.00001",
        "Release.00002/movie.mkv",
        "Release.00002",
    }
    assert set(delta["removed"]) == {"Release.00003", "Release.00003/movie.mkv"}

    # The actual point: payload size is bounded by what changed, not by `n`.
    assert len(json.dumps(delta)) < 2000


async def test_scan_delta_payload_does_not_scale_with_tree_size(tmp_path, monkeypatch):
    """The direct proof: run the identical 2-file mutation against a small tree and a tree
    250x larger, and assert the *delta* payload barely moves — while the *full snapshot*
    (what phase 2 sent on every scan, and what a naive re-implementation could regress to)
    scales with `n` exactly as expected. If the delta fix ever regresses to resending whole
    trees, this is the assertion that catches it.
    """
    small_delta, small_snapshot = await _mutated_delta_payload(tmp_path / "small", monkeypatch, 20)
    (tmp_path / "big").mkdir()
    big_delta, big_snapshot = await _mutated_delta_payload(tmp_path / "big", monkeypatch, 5000)

    small_delta_bytes = len(json.dumps(small_delta))
    big_delta_bytes = len(json.dumps(big_delta))
    small_snapshot_bytes = len(json.dumps(small_snapshot))
    big_snapshot_bytes = len(json.dumps(big_snapshot))

    # The delta barely grows even though the tree is 250x bigger (same 4 changed + 2 removed
    # rows either way) -- a small, constant allowance for id/formatting noise, nothing more.
    assert big_delta_bytes - small_delta_bytes < 200, (
        f"delta payload grew with tree size: {small_delta_bytes} -> {big_delta_bytes} bytes "
        f"for a 20 -> 5000 item tree; deltas must be proportional to what changed"
    )

    # Contrast: the full snapshot (sent once, on connect) *does* scale with tree size --
    # confirms the fixture is actually exercising a tree big enough to matter, and that the
    # delta's flatness above isn't just an artifact of a trivially small fixture.
    assert big_snapshot_bytes > small_snapshot_bytes * 100

    # And the delta is tiny next to even the *small* tree's own full snapshot.
    assert big_delta_bytes < small_snapshot_bytes


async def test_ws_nodes_carry_item_id_so_the_ui_can_act_on_them(tmp_path, monkeypatch):
    """Every node the WebSocket sends must carry its `item.id`.

    The Files page renders purely from this stream — never from `GET /api/files` — and every
    action it offers (Queue, Stop, bulk ops) addresses an item by id. When the engine's
    serializer omitted it, every row arrived with `id == null` and the UI rendered no action
    button at all, on every row: a REMOTE_ONLY file could be seen but never queued. Found
    against a real deployment, because phase 3b verified the API and WS contract with curl and
    a raw socket client but never clicked the button. Now structurally impossible to
    reintroduce — the projection is built from the `item` row, so the id is its primary key
    (`core/itemview.py`).
    """
    monkeypatch.setattr(engine_module.local_scan, "scan_local", lambda root: {})  # noqa: ARG005

    tree = _release_tree(3)
    engine, q, host, db = await _make_engine(tmp_path, [tree, tree])
    try:
        subscription = engine.events.subscribe()
        await engine.scan_queue(q, host)
        delta = await subscription.get()

        assert delta["type"] == "queue_delta"
        assert delta["changed"], "expected a first-scan delta carrying the whole tree"
        for node in delta["changed"]:
            assert node.get("id") is not None, f"delta node has no id: {node['rel_path']}"

        for message in await engine.snapshot():
            assert message["nodes"], "expected nodes in the snapshot"
            for node in message["nodes"]:
                assert node.get("id") is not None, f"snapshot node has no id: {node['rel_path']}"
    finally:
        await db.close()


# --- The wire agrees with the database (DESIGN.md §2/§9; core/itemview.py) ------------------


def _flat_tree(n: int) -> dict[str, RemoteEntry]:
    """`n` filler files plus the four the override tests act on -- flat rather than
    `_release_tree`'s directories so a seeded state belongs to exactly one row and no parent
    rolls up alongside it.
    """
    tree = {
        name: RemoteEntry(rel_path=name, is_dir=False, size=1_000, mtime=1.0)
        for name in ("postprocessed.mkv", "lifecycle.mkv", "removed.mkv", "plain.mkv")
    }
    for i in range(n):
        name = f"filler-{i:05d}.bin"
        tree[name] = RemoteEntry(rel_path=name, is_dir=False, size=1_000 + i, mtime=1.0)
    return tree


def _mirrored_locally(tree: dict[str, RemoteEntry], *, absent: set[str]) -> dict[str, LocalEntry]:
    return {
        p: LocalEntry(rel_path=p, is_dir=e.is_dir, size=e.size)
        for p, e in tree.items()
        if p not in absent
    }


async def _persisted_states(db, queue_id: int) -> dict[str, str]:
    cursor = await db.execute("SELECT rel_path, state FROM item WHERE queue_id = ?", (queue_id,))
    return {row["rel_path"]: row["state"] for row in await cursor.fetchall()}


def _assert_wire_matches_db(nodes: list[dict], persisted: dict[str, str]) -> None:
    """The invariant, asserted directly: for every published node, the state on the wire is
    the state in the `item` table. Nothing publishes a value it did not read back.
    """
    for node in nodes:
        assert node["state"] == persisted[node["rel_path"]], (
            f"{node['rel_path']}: wire says {node['state']!r}, "
            f"database says {persisted[node['rel_path']]!r}"
        )


@pytest.mark.parametrize("n", [20, 5000])
async def test_published_state_is_the_persisted_state_not_the_structural_one(
    tmp_path, monkeypatch, n
):
    """The bug this file's projection exists to close, at every level at once.

    `core/engine.py._persist` is where an item's state is really decided — `core/queue.py`'s
    job-lifecycle protection, §6's post-processing precedence, §7.3's `REMOVED_LOCAL` grace
    period — and none of it used to reach the wire, because the delta was computed from
    `core/reconcile.py`'s structural nodes *before* any of that arbitration ran. Three of the
    four items below have a persisted state their structural reading disagrees with; the
    fourth is the plain case that must keep behaving exactly as it always did.

    Also the delta-size property (phase 3b) under the new diff: making effective-state changes
    visible to the diff for the first time must not make the delta scale with tree size. Only
    the three items whose *persisted* state changed are sent, out of `n + 4` — with `n` at
    both 20 and 5000.
    """
    tree = _flat_tree(n)
    absent = {"removed.mkv"}
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root: _mirrored_locally(tree, absent=absent),  # noqa: ARG005
    )

    engine, q, host, db = await _make_engine(tmp_path, [tree, tree])
    try:
        subscription = engine.events.subscribe()
        await engine.scan_queue(q, host)  # baseline: creates the `item` rows
        await subscription.get()

        # Seed the three overrides the way their real owners would, then rescan. The
        # structural reading for all three is unchanged from the baseline scan, which is
        # exactly why the old code published nothing at all for them.
        cursor = await db.execute("SELECT id, rel_path FROM item WHERE queue_id = ?", (q.id,))
        ids = {row["rel_path"]: row["id"] for row in await cursor.fetchall()}

        # 1. A post-processing outcome (§6): structurally DOWNLOADED, persisted EXTRACTED.
        await db.execute(
            "UPDATE item SET state = 'EXTRACTED' WHERE id = ?", (ids["postprocessed.mkv"],)
        )
        # 2. A protected job-lifecycle state (§4.6): structurally DOWNLOADED, persisted
        #    DOWNLOADING because a job is running for it.
        await db.execute(
            "UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (ids["lifecycle.mkv"],)
        )
        await db.execute(
            "INSERT INTO job (item_id, kind, state, lane) VALUES (?, 'pget', 'running', 'main')",
            (ids["lifecycle.mkv"],),
        )
        # 3. A §7.3 grace period about to expire: structurally REMOTE_ONLY (locally absent),
        #    and the clock backdated past the window so this scan lands it on REMOVED_LOCAL.
        #    Nothing pushes an `item_delta` for this transition -- the scan path is its only
        #    route to the wire.
        await db.execute(
            "UPDATE item SET state = 'DOWNLOADED', "
            "first_missing_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE id = ?",
            (f"-{int(DEFAULT_GRACE_S) + 60} seconds", ids["removed.mkv"]),
        )
        await db.commit()

        await engine.scan_queue(q, host)
        delta = await subscription.get()
        persisted = await _persisted_states(db, q.id)

        assert persisted["postprocessed.mkv"] == "EXTRACTED"
        assert persisted["lifecycle.mkv"] == "DOWNLOADING"
        assert persisted["removed.mkv"] == "REMOVED_LOCAL"
        assert persisted["plain.mkv"] == "DOWNLOADED"

        _assert_wire_matches_db(delta["changed"], persisted)
        assert {node["rel_path"] for node in delta["changed"]} == {
            "postprocessed.mkv",
            "lifecycle.mkv",
            "removed.mkv",
        }, "only the rows whose persisted state changed belong in the delta"
        assert len(json.dumps(delta)) < 2000, "the delta must stay proportional to what changed"

        # ...and the connect-time snapshot -- the reload path, which is how this bug was
        # actually visible to a user -- agrees with the database for every node, not just the
        # ones that changed.
        snapshot = (await engine.snapshot())[0]
        assert len(snapshot["nodes"]) == n + 4
        _assert_wire_matches_db(snapshot["nodes"], persisted)
    finally:
        await db.close()


async def test_snapshot_reflects_a_lifecycle_write_made_since_the_last_scan(tmp_path, monkeypatch):
    """A client connecting between scans must not be handed a model older than the database.

    `core/queue.py` and `core/postprocess.py` write `item.state` the instant a job or a
    pipeline step moves and push an `item_delta` for it -- but a browser that connects *after*
    that push has no way to receive it, and the next scan can be up to `scan_interval_s`
    (default 30s) away. `snapshot()` therefore re-reads the `item` table rather than serving
    the cached model, which is why it is `async`.
    """
    tree = _flat_tree(0)
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root: _mirrored_locally(tree, absent=set()),  # noqa: ARG005
    )

    engine, q, host, db = await _make_engine(tmp_path, [tree])
    try:
        await engine.scan_queue(q, host)

        # What `TransferQueue.enqueue_item` does, with no scan following it.
        await db.execute(
            "UPDATE item SET state = 'QUEUED' WHERE queue_id = ? AND rel_path = 'plain.mkv'",
            (q.id,),
        )
        await db.commit()

        snapshot = (await engine.snapshot())[0]
        states = {node["rel_path"]: node["state"] for node in snapshot["nodes"]}
        assert states["plain.mkv"] == "QUEUED"
        _assert_wire_matches_db(snapshot["nodes"], await _persisted_states(db, q.id))
    finally:
        await db.close()
