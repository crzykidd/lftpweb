"""`local_mtime` (migration 011, 2026-08-13, prompts/2026-08-13-files-detail-inspector.md)
actually reaching the database through `core/engine.py._persist`.

`tests/test_local_scan.py` covers capture (filesystem -> `LocalEntry`) and
`tests/test_reconcile.py` covers the merge (`LocalEntry` -> `ReconciledNode`); this file is the
third and last leg -- `ReconciledNode` -> the `item` row, on both of `_persist`'s write paths
(the "protected" row branch and the ordinary structural branch), and cleared, like `local_size`,
when a `rel_path` leaves both trees entirely (the "vanished" branch).
"""

from __future__ import annotations

import lftpweb.core.engine as engine_module
from lftpweb.core.local_scan import LocalEntry

from test_state_persistence import REL_PATH, SIZE, _make_engine, _make_move_engine

LOCAL_MTIME = 1_700_000_000.0


def _local_tree_with_mtime(size: int, mtime: float) -> dict[str, LocalEntry]:
    return {REL_PATH: LocalEntry(rel_path=REL_PATH, is_dir=False, size=size, mtime=mtime)}


async def _local_mtime_of(db, item_id: int) -> float | None:
    cursor = await db.execute("SELECT local_mtime FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return float(row["local_mtime"]) if row["local_mtime"] is not None else None


async def test_local_mtime_persisted_on_an_ordinary_scan(tmp_path, monkeypatch):
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state="REMOTE_ONLY"
    )
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: _local_tree_with_mtime(SIZE, LOCAL_MTIME),  # noqa: ARG005
    )
    try:
        await engine.scan_queue(q, host)
        assert await _local_mtime_of(db, item_id) == LOCAL_MTIME
    finally:
        await db.close()


async def test_local_mtime_refreshed_even_on_a_protected_row(tmp_path, monkeypatch):
    """A queued/downloading item's `state` is protected (`core/queue.py` owns it), but its
    size/mtime columns still refresh on every pass -- `_persist`'s "protected" branch has
    always done this for `remote_mtime`; `local_mtime` must behave identically.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE // 2, state="DOWNLOADING"
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane) VALUES (?, 'pget', 'running', 'main')",
        (item_id,),
    )
    await db.commit()
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: _local_tree_with_mtime(SIZE // 2, LOCAL_MTIME),  # noqa: ARG005
    )
    try:
        await engine.scan_queue(q, host)
        assert await _local_mtime_of(db, item_id) == LOCAL_MTIME
    finally:
        await db.close()


async def test_local_mtime_updates_across_repeated_scans(tmp_path, monkeypatch):
    """Not latched (the same discipline `core/reconcile.py`'s own docstring requires of
    `remote_size` -- rule 4) -- a file rewritten between scans reports its new mtime, not a
    value cached from the first pass.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state="DOWNLOADED"
    )
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: _local_tree_with_mtime(SIZE, LOCAL_MTIME),  # noqa: ARG005
    )
    try:
        await engine.scan_queue(q, host)
        assert await _local_mtime_of(db, item_id) == LOCAL_MTIME

        later = LOCAL_MTIME + 3600
        monkeypatch.setattr(
            engine_module.local_scan,
            "scan_local",
            lambda root, **_kwargs: _local_tree_with_mtime(SIZE, later),  # noqa: ARG005
        )
        await engine.scan_queue(q, host)
        assert await _local_mtime_of(db, item_id) == later
    finally:
        await db.close()


async def test_local_mtime_cleared_when_the_item_vanishes_from_both_trees(tmp_path, monkeypatch):
    """The "vanished from both trees" branch already NULLs `remote_size`/`local_size` for a
    `rel_path` `core/reconcile.py` produced no node for at all (§3.2 rule 3's move-mode/importer
    case, `core/engine.py._persist`'s own docstring) -- `local_mtime` must go with them. A
    stale reading surviving would claim a "modified" time for a file that no longer exists on
    disk at all.
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path,
        monkeypatch,
        local_size=SIZE,
        state="EXTRACTED",
        remote_deleted_at="2026-08-13T00:00:00.000000Z",
    )
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: _local_tree_with_mtime(SIZE, LOCAL_MTIME),  # noqa: ARG005
    )
    try:
        await engine.scan_queue(q, host)
        assert await _local_mtime_of(db, item_id) == LOCAL_MTIME

        # The local copy leaves too (auto_move relocated it, or an importer took it) -- the
        # rel_path is now in neither tree.
        monkeypatch.setattr(engine_module.local_scan, "scan_local", lambda root, **_kwargs: {})  # noqa: ARG005
        await engine.scan_queue(q, host)
        assert await _local_mtime_of(db, item_id) is None
    finally:
        await db.close()
