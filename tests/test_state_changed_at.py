"""Migration 006: `item.state_changed_at`, "when did this row's state last actually change"
(prompts/done/2026-08-12-state-changed-at.md, DESIGN.md §9.2's Files page). Enforced with two
triggers rather than writer discipline, because `item.state` is written from three separate
modules (`core/engine.py._persist`, `core/queue.py`, `core/postprocess.py`) -- see the
migration file's own docstring and docs/decisions.md for why.

These tests exercise the triggers' actual behaviour, not just their existence: that the
`AFTER UPDATE OF state` trigger fires on the `ON CONFLICT DO UPDATE` branch `_persist` uses for
most transitions, that it does NOT fire when a rescan persists the same state (the routine
30s-pass case), that it cannot re-enter even with `recursive_triggers` forced on, and that the
`AFTER INSERT` trigger stamps a brand new row without relying on a column `DEFAULT` (SQLite
refuses a non-constant `ADD COLUMN ... DEFAULT` the moment a table has rows -- proven directly
in the backfill tests below, which is exactly the situation this migration runs into on every
real database).
"""

from __future__ import annotations

import shutil

import aiosqlite
import pytest

import lftpweb.db as db_module
from lftpweb.db import connect, migrate

# The exact shape of `core/engine.py._persist`'s second upsert (the unprotected-item branch,
# where most state transitions actually land) -- copied rather than imported, since it's a
# private statement inline in `Engine._persist`, not a helper this test could call directly
# without standing up a whole `Engine`. Fidelity to the real SQL is what makes this test mean
# something; see `core/engine.py` lines ~572-596.
_PERSIST_UPSERT = """
    INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate, first_missing_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (queue_id, rel_path) DO UPDATE SET
        is_dir = excluded.is_dir,
        remote_size = excluded.remote_size,
        local_size = excluded.local_size,
        remote_mtime = excluded.remote_mtime,
        state = excluded.state,
        substate = excluded.substate,
        first_missing_at = excluded.first_missing_at
"""


async def _persist_upsert(db, queue_id: int, rel_path: str, state: str) -> None:
    await db.execute(
        _PERSIST_UPSERT,
        (queue_id, rel_path, 0, 100, 100, "1.0", state, None, None),
    )
    await db.commit()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)  # real head, migration 006 included
    yield conn
    await conn.close()


async def _make_queue(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'example.invalid', 'u', 'key', 'insecure')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _state_changed_at(db, item_id: int) -> str | None:
    cursor = await db.execute("SELECT state_changed_at FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return row["state_changed_at"]


# --- schema shape ----------------------------------------------------------------------------


async def test_migration_adds_column_and_both_triggers(db):
    cursor = await db.execute("PRAGMA table_info(item)")
    columns = {row["name"] for row in await cursor.fetchall()}
    assert "state_changed_at" in columns

    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'item'"
    )
    triggers = {row["name"] for row in await cursor.fetchall()}
    assert {"item_state_changed_at", "item_state_changed_at_insert"}.issubset(triggers)


# --- AFTER INSERT: new rows get a value without a column DEFAULT ----------------------------


async def test_new_row_is_stamped_at_insert_time(db):
    queue_id = await _make_queue(db)
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state) VALUES (?, 'Release', 0, 'REMOTE_ONLY')",
        (queue_id,),
    )
    await db.commit()
    assert await _state_changed_at(db, cursor.lastrowid) is not None


# --- AFTER UPDATE OF state: the ON CONFLICT DO UPDATE branch _persist actually uses ----------


async def test_fires_on_the_on_conflict_do_update_branch_when_state_changes(db):
    queue_id = await _make_queue(db)
    await _persist_upsert(db, queue_id, "Release", "REMOTE_ONLY")
    cursor = await db.execute(
        "SELECT id FROM item WHERE queue_id = ? AND rel_path = 'Release'", (queue_id,)
    )
    item_id = (await cursor.fetchone())["id"]
    first_stamp = await _state_changed_at(db, item_id)
    assert first_stamp is not None

    await db.execute(
        "UPDATE item SET state_changed_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (item_id,)
    )
    await db.commit()
    baseline = await _state_changed_at(db, item_id)
    assert baseline == "2000-01-01T00:00:00.000000Z"

    # Same upsert, same conflicting row, DIFFERENT state -- the ON CONFLICT DO UPDATE branch,
    # not a fresh insert.
    await _persist_upsert(db, queue_id, "Release", "QUEUED")
    after = await _state_changed_at(db, item_id)
    assert after != baseline
    assert after is not None


async def test_does_not_fire_when_a_rescan_persists_the_same_state(db):
    """The routine case: a quiet item's 30s rescan re-upserts identical values, `state`
    included. The clock must not move, or every item in a database would show "changed
    seconds ago" forever regardless of whether anything actually happened.
    """
    queue_id = await _make_queue(db)
    await _persist_upsert(db, queue_id, "Release", "DOWNLOADED")
    cursor = await db.execute(
        "SELECT id FROM item WHERE queue_id = ? AND rel_path = 'Release'", (queue_id,)
    )
    item_id = (await cursor.fetchone())["id"]
    baseline = await _state_changed_at(db, item_id)
    assert baseline is not None

    # Re-persist the exact same state, as a rescan of an unchanged item would.
    await _persist_upsert(db, queue_id, "Release", "DOWNLOADED")
    assert await _state_changed_at(db, item_id) == baseline


async def test_plain_update_from_queue_py_also_stamps_on_change(db):
    """`core/queue.py` writes `state` with plain `UPDATE item SET state = ? WHERE id = ?`,
    never through `_persist`'s upsert -- the trigger must fire for that shape too.
    """
    queue_id = await _make_queue(db)
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state) VALUES (?, 'Release', 0, 'QUEUED')",
        (queue_id,),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await db.execute(
        "UPDATE item SET state_changed_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (item_id,)
    )
    await db.commit()

    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (item_id,))
    await db.commit()
    assert await _state_changed_at(db, item_id) != "2000-01-01T00:00:00.000000Z"


async def test_plain_update_that_reassigns_the_same_state_does_not_stamp(db):
    """`core/queue.py._sample_and_publish_progress`'s per-child CASE statement always assigns
    `state` in its SET clause, even on ticks where the computed value is unchanged -- the
    trigger's `WHEN NEW.state IS NOT OLD.state` guard, not merely "state wasn't in the SET
    clause," is what must suppress this.
    """
    queue_id = await _make_queue(db)
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state) VALUES (?, 'Release', 0, 'DOWNLOADING')",
        (queue_id,),
    )
    item_id = cursor.lastrowid
    await db.commit()
    baseline = await _state_changed_at(db, item_id)
    assert baseline is not None

    # `state = state` -- the column is touched by the statement, but the value doesn't move.
    await db.execute("UPDATE item SET state = state WHERE id = ?", (item_id,))
    await db.commit()
    assert await _state_changed_at(db, item_id) == baseline


# --- cannot re-enter --------------------------------------------------------------------------


async def test_trigger_cannot_reenter_even_with_recursive_triggers_forced_on(db):
    """The trigger's own UPDATE touches only `state_changed_at`, never `state` -- so it cannot
    re-fire `item_state_changed_at` regardless of the `recursive_triggers` pragma. Forcing that
    pragma ON (SQLite's own default is OFF, which would mask a design that only happened to be
    safe by accident) and asserting the UPDATE below completes without SQLite's "too many
    levels of trigger recursion" error is the actual proof; it is not a timing-based test.
    """
    await db.execute("PRAGMA recursive_triggers = ON")
    queue_id = await _make_queue(db)
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state) VALUES (?, 'Release', 0, 'REMOTE_ONLY')",
        (queue_id,),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await db.execute("UPDATE item SET state = 'QUEUED' WHERE id = ?", (item_id,))
    await db.commit()

    cursor = await db.execute("SELECT state, state_changed_at FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["state"] == "QUEUED"
    assert row["state_changed_at"] is not None


# --- backfill ----------------------------------------------------------------------------------


@pytest.fixture
async def staged_at_005(tmp_path, monkeypatch):
    """A migrations directory holding only 001-005 -- "a database at 005" as the handoff
    prompt asks for -- so `item` rows can be seeded with pre-006 timestamp combinations before
    006 (and its backfill) ever runs. Mirrors `test_db.py`'s own pattern of pointing
    `db_module.MIGRATIONS_DIR` at a throwaway directory rather than mutating the real one.
    """
    real_migrations = db_module.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    for name in (
        "001_initial_schema.sql",
        "002_phase4_patterns_only.sql",
        "003_phase5_postprocess.sql",
        "004_phase8_auth.sql",
        "005_throughput_metrics.sql",
    ):
        shutil.copy(real_migrations / name, staged / name)
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", staged)

    conn = await connect(str(tmp_path))
    await migrate(conn)  # database at migration 5, no state_changed_at column yet

    yield conn, staged, real_migrations
    await conn.close()


async def test_migration_006_backfill_prefers_extracted_then_verified_then_downloaded(
    staged_at_005,
):
    conn, staged, real_migrations = staged_at_005

    cursor = await conn.execute(
        "INSERT INTO host (name, address, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'example.invalid', 'u', 'key', 'insecure')"
    )
    host_id = cursor.lastrowid
    cursor = await conn.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    queue_id = cursor.lastrowid

    # Four rows, one per rung of the COALESCE ladder migration 006 uses.
    await conn.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, first_seen_at, downloaded_at, "
        "verified_at, extracted_at) VALUES "
        "(?, 'has-extracted', 0, 'EXTRACTED', '2020-01-01T00:00:00.000000Z', "
        "'2020-01-02T00:00:00.000000Z', '2020-01-03T00:00:00.000000Z', '2020-01-04T00:00:00.000000Z')",
        (queue_id,),
    )
    await conn.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, first_seen_at, downloaded_at, "
        "verified_at) VALUES "
        "(?, 'has-verified', 0, 'VERIFIED', '2020-01-01T00:00:00.000000Z', "
        "'2020-01-02T00:00:00.000000Z', '2020-01-03T00:00:00.000000Z')",
        (queue_id,),
    )
    await conn.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, first_seen_at, downloaded_at) "
        "VALUES (?, 'has-downloaded', 0, 'DOWNLOADED', '2020-01-01T00:00:00.000000Z', "
        "'2020-01-02T00:00:00.000000Z')",
        (queue_id,),
    )
    await conn.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, first_seen_at) "
        "VALUES (?, 'remote-only', 0, 'REMOTE_ONLY', '2020-01-01T00:00:00.000000Z')",
        (queue_id,),
    )
    await conn.commit()

    shutil.copy(real_migrations / "006_state_changed_at.sql", staged / "006_state_changed_at.sql")
    await migrate(conn)

    async def _value(rel_path: str) -> str | None:
        cursor = await conn.execute(
            "SELECT state_changed_at FROM item WHERE queue_id = ? AND rel_path = ?",
            (queue_id, rel_path),
        )
        return (await cursor.fetchone())["state_changed_at"]

    assert await _value("has-extracted") == "2020-01-04T00:00:00.000000Z"
    assert await _value("has-verified") == "2020-01-03T00:00:00.000000Z"
    assert await _value("has-downloaded") == "2020-01-02T00:00:00.000000Z"
    assert (
        await _value("remote-only") == "2020-01-01T00:00:00.000000Z"
    )  # falls back to first_seen_at
