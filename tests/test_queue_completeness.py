"""`core/queue.py._reap_one`'s filesystem completeness check on exit 0 (2026-08-14,
prompts/2026-08-14-exit-zero-is-not-completion.md) -- the core fix for a live incident: job 43
exited 0 (`cmd:fail-exit true`) having left one file 500 MB short as a `.lftp` temp file, and the
item was marked `DOWNLOADED` and handed to post-processing anyway.

No seedbox needed -- like `tests/test_queue_child_progress.py`, nothing here spawns a real lftp
process. `_reap_one` is exercised directly against a hand-built `_RunningProcess` whose
`wait_task` is an already-resolved `asyncio.Task` (real exit code + output, no process behind
it) and whose `spawned` is a `lftp.SpawnedJob` pointed at files that don't exist, so
`.cleanup()` (an idempotent unlink, never raises on a missing path) is a safe no-op.

The settle gate is disabled in the `db` fixture, same as `tests/test_queue.py`'s own fixture and
for the same reason: these tests are about the *completeness* gate, a different question
("did we actually get it all?") from the settle gate's ("has the remote stopped changing?"),
and leaving the settle gate on would hold every item at REMOTE_ONLY/settling regardless of what
this task changed, masking exactly what these tests need to isolate.
"""

from __future__ import annotations

import asyncio

import pytest

from lftpweb.core import lftp
from lftpweb.core.queue import _RunningProcess
from lftpweb.core.settle import SettleSettings, save_settle_settings
from test_queue import _make_db, _make_host_row, _make_item_row, _make_queue_row, _queue_for


@pytest.fixture
async def db(tmp_path):
    conn = await _make_db(tmp_path)
    await save_settle_settings(conn, SettleSettings(enabled=False))
    try:
        yield conn
    finally:
        await conn.close()


class _FakePostprocess:
    """Records every `trigger()` call instead of actually running verify/delete/extract/move --
    exactly what the "never reaches the delete gate" assertions need: if `trigger` was never
    called, the whole pipeline (including the `move`-mode remote delete) never ran.
    """

    def __init__(self) -> None:
        self.triggered: list[int] = []

    def trigger(self, item_id: int) -> None:
        self.triggered.append(item_id)

    def in_flight_item_ids(self) -> frozenset[int]:
        return frozenset()


def _fake_spawned(tmp_path, job_id: int) -> lftp.SpawnedJob:
    # Points at paths that don't exist -- `.cleanup()` (`Path.unlink(missing_ok=True)`) is a
    # documented no-op for that, never a real credential file this task's fixtures need to
    # actually create.
    return lftp.SpawnedJob(
        proc=None,  # type: ignore[arg-type] -- never touched by `_reap_one`'s own code path
        pid=10_000 + job_id,
        rc_path=tmp_path / f"job-{job_id}.rc",
        known_hosts_path=None,
    )


def _resolved_wait_task(exit_code: int, tail: str) -> asyncio.Task:
    async def _wait() -> tuple[int, str]:
        return exit_code, tail

    return asyncio.create_task(_wait())


async def _make_job_row(db, *, job_id: int, item_id: int, kind: str) -> None:
    """A `job` row `_reap_one`'s own `UPDATE ... WHERE id = ?` can actually land on -- without
    this the UPDATE silently matches zero rows and every "read the job back" assertion below
    would be checking a row that was never inserted, passing for the wrong reason.
    """
    await db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (?, ?, ?, 'running', 'main', 0, 1)",
        (job_id, item_id, kind),
    )
    await db.commit()


def _mirror_proc(
    *,
    job_id: int,
    item_id: int,
    queue_id: int,
    rel_path: str,
    local_root,
    bytes_total: int,
    tmp_path,
) -> _RunningProcess:
    return _RunningProcess(
        job_id=job_id,
        item_id=item_id,
        queue_id=queue_id,
        rel_path=rel_path,
        is_dir=True,
        kind="mirror",
        lane="main",
        rate_limit_bps=0,
        forced_rate_fraction=None,
        local_root=str(local_root),
        bytes_total=bytes_total,
        remote_mtime=None,
        spawned=_fake_spawned(tmp_path, job_id),
        wait_task=_resolved_wait_task(0, "some real lftp output, kept now (defect 2)"),
    )


def _pget_proc(
    *,
    job_id: int,
    item_id: int,
    queue_id: int,
    rel_path: str,
    local_root,
    bytes_total: int,
    tmp_path,
) -> _RunningProcess:
    return _RunningProcess(
        job_id=job_id,
        item_id=item_id,
        queue_id=queue_id,
        rel_path=rel_path,
        is_dir=False,
        kind="pget",
        lane="main",
        rate_limit_bps=0,
        forced_rate_fraction=None,
        local_root=str(local_root),
        bytes_total=bytes_total,
        remote_mtime=None,
        spawned=_fake_spawned(tmp_path, job_id),
        wait_task=_resolved_wait_task(0, "some real lftp output"),
    )


async def _item_row(db, item_id):
    cursor = await db.execute(
        "SELECT state, substate, local_size, auto_queue_suppressed FROM item WHERE id = ?",
        (item_id,),
    )
    return await cursor.fetchone()


async def _job_row(db, job_id):
    cursor = await db.execute(
        "SELECT state, output_tail, exit_code FROM job WHERE id = ?", (job_id,)
    )
    return await cursor.fetchone()


async def _event_rows(db, item_id, kind):
    cursor = await db.execute(
        "SELECT level, message FROM event WHERE item_id = ? AND kind = ?", (item_id, kind)
    )
    return await cursor.fetchall()


# --- the real-incident shape: a mirror job, one file left as a plain `.lftp` temp file --------


async def test_leftover_plain_lftp_temp_file_holds_the_item_at_partial_not_downloaded(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    release_dir = local_dir / "testfolder10"
    release_dir.mkdir()

    # Two files, 1000 bytes each, matching the live incident's shape (one whole, one short and
    # still under its `.lftp` name -- job 43 left `S.W.A.T.S06E22….mkv.lftp` on disk).
    item_id = await _make_item_row(db, queue_id, "testfolder10", is_dir=True, remote_size=2000)
    a_id = await _make_item_row(db, queue_id, "testfolder10/a.mkv", is_dir=False, remote_size=1000)
    b_id = await _make_item_row(db, queue_id, "testfolder10/b.mkv", is_dir=False, remote_size=1000)
    await db.commit()
    (release_dir / "a.mkv").write_bytes(b"a" * 1000)
    (release_dir / "b.mkv.lftp").write_bytes(b"b" * 500)  # 500 of 1000 -- short, still temp-named

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=43, item_id=item_id, kind="mirror")
    proc = _mirror_proc(
        job_id=43,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="testfolder10",
        local_root=release_dir,
        bytes_total=2000,
        tmp_path=tmp_path,
    )
    q._running[43] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "PARTIAL", "exit 0 must not be enough on its own -- the disk disagrees"
    assert item["substate"] is None
    assert item["auto_queue_suppressed"] == 0, "must stay eligible for auto-queue's re-pick-up"
    assert item["local_size"] == 1500  # 1000 whole + 500 effective on the still-temp file

    job = await _job_row(db, 43)
    assert job["state"] == "succeeded"  # lftp really did exit 0 -- that part is not in question
    assert job["output_tail"] is not None, "defect 2: the evidence must survive, not be nulled"

    events = await _event_rows(db, item_id, "incomplete_on_exit_zero")
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert "1500" in events[0]["message"] and "2000" in events[0]["message"]
    assert "b.mkv.lftp" in events[0]["message"]

    assert q.postprocess.triggered == [], "post-processing must never fire for an incomplete item"

    # b.mkv's own child row must also read PARTIAL -- _flush_child_progress_final already does
    # this correctly; the parent-level completeness gate must not contradict it.
    b_row = await db.execute("SELECT state, local_size FROM item WHERE id = ?", (b_id,))
    b = await b_row.fetchone()
    assert b["state"] == "PARTIAL"
    assert b["local_size"] == 500
    a_row = await db.execute("SELECT state FROM item WHERE id = ?", (a_id,))
    assert (await a_row.fetchone())["state"] == "DOWNLOADED"


# --- the timestamped variant: `<name>.lftp~<timestamp>~`, not just plain `.lftp` --------------


async def test_leftover_timestamped_temp_file_variant_also_holds_the_item_at_partial(db, tmp_path):
    """`local_scan.TEMP_FILE_RE` matches both `.lftp` and lftp's `<name>.lftp~<timestamp>~`
    fallback (DESIGN.md §4.4b's documented variant) -- a completeness check that only matched
    the plain suffix would miss exactly this case, the retry case most likely to hit it.
    """
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    release_dir = local_dir / "Release"
    release_dir.mkdir()

    item_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1000)
    await _make_item_row(db, queue_id, "Release/only.mkv", is_dir=False, remote_size=1000)
    await db.commit()
    (release_dir / "only.mkv.lftp~20260813154311~").write_bytes(b"x" * 1000)

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=1, item_id=item_id, kind="mirror")
    proc = _mirror_proc(
        job_id=1,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="Release",
        local_root=release_dir,
        bytes_total=1000,
        tmp_path=tmp_path,
    )
    q._running[1] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "PARTIAL"
    events = await _event_rows(db, item_id, "incomplete_on_exit_zero")
    assert len(events) == 1
    assert "only.mkv.lftp~20260813154311~" in events[0]["message"]
    assert q.postprocess.triggered == []


# --- an orphaned `.lftp-pget-status` sidecar counts too, even when the byte total is met ------


async def test_orphaned_pget_status_sidecar_holds_the_item_at_partial_even_at_full_byte_count(
    db, tmp_path
):
    """`local_scan.find_orphan_sidecars`: a sidecar whose carrier file isn't present *at all* in
    the same directory -- not the plain name, not any `.lftp` temp variant -- is dangling
    bookkeeping `scan_local`'s own output has no way to see (it's only ever consumed when a
    matching carrier is found alongside it). The item's *counted* content (`a.mkv`) is fully
    present and matches the remote total on its own, so the byte-total half of the check alone
    would call this complete -- the leftover sidecar is the only signal, and it must still hold
    the item at `PARTIAL`.
    """
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    release_dir = local_dir / "Release"
    release_dir.mkdir()

    item_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1000)
    await _make_item_row(db, queue_id, "Release/a.mkv", is_dir=False, remote_size=1000)
    await db.commit()
    (release_dir / "a.mkv").write_bytes(b"a" * 1000)  # the whole remote total, on its own
    # A dangling sidecar with no carrier anywhere in the directory -- e.g. left behind by an
    # interrupted process from a previous attempt at a file that no longer exists in any form.
    (release_dir / "ghost.mkv.lftp-pget-status").write_text("size=1000\n0.pos=1000\n0.limit=1000\n")

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=1, item_id=item_id, kind="mirror")
    proc = _mirror_proc(
        job_id=1,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="Release",
        local_root=release_dir,
        bytes_total=1000,
        tmp_path=tmp_path,
    )
    q._running[1] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "PARTIAL"
    events = await _event_rows(db, item_id, "incomplete_on_exit_zero")
    assert len(events) == 1
    assert "ghost.mkv.lftp-pget-status" in events[0]["message"]
    assert q.postprocess.triggered == []


# --- the same check for a `pget` job (a single loose file, not a mirrored directory) ----------


async def test_pget_job_short_temp_file_holds_the_item_at_partial(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)

    item_id = await _make_item_row(db, queue_id, "loose.iso", is_dir=False, remote_size=1000)
    await db.commit()
    (local_dir / "loose.iso.lftp").write_bytes(b"x" * 400)

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=1, item_id=item_id, kind="pget")
    proc = _pget_proc(
        job_id=1,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="loose.iso",
        local_root=local_dir / "loose.iso",
        bytes_total=1000,
        tmp_path=tmp_path,
    )
    q._running[1] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "PARTIAL"
    events = await _event_rows(db, item_id, "incomplete_on_exit_zero")
    assert len(events) == 1
    assert "loose.iso.lftp" in events[0]["message"]
    assert q.postprocess.triggered == []


# --- positive control: a genuinely complete transfer still reaches DOWNLOADED -----------------


async def test_genuinely_complete_transfer_still_reaches_downloaded_and_triggers_postprocess(
    db, tmp_path
):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    release_dir = local_dir / "Release"
    release_dir.mkdir()

    item_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1000)
    await _make_item_row(db, queue_id, "Release/only.mkv", is_dir=False, remote_size=1000)
    await db.commit()
    (release_dir / "only.mkv").write_bytes(b"x" * 1000)

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=1, item_id=item_id, kind="mirror")
    proc = _mirror_proc(
        job_id=1,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="Release",
        local_root=release_dir,
        bytes_total=1000,
        tmp_path=tmp_path,
    )
    q._running[1] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "DOWNLOADED"
    assert await _event_rows(db, item_id, "incomplete_on_exit_zero") == []
    assert q.postprocess.triggered == [item_id]

    job = await _job_row(db, 1)
    assert job["state"] == "succeeded"
    assert (
        job["output_tail"] is not None
    ), "output_tail is retained for every success now (defect 2), not just the incomplete case"


# --- a `move`-mode item in this state never reaches the delete gate ---------------------------


async def test_incomplete_move_mode_item_never_reaches_the_delete_gate(db, tmp_path):
    """The delete gate lives inside `core/postprocess.py`'s pipeline, reached only via
    `TransferQueue.postprocess.trigger()` (DESIGN.md §6's "two call sites, both narrow"). An
    incomplete item must never reach that call at all -- proven here the same way the other
    tests prove it, by asserting `trigger` was never invoked, on a queue actually configured
    for `move` mode (the mode where reaching the delete gate would be irreversible).
    """
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'ar-tv', '/data/pickup', ?, 1, 'move')",
        (host_id, str(local_dir)),
    )
    await db.commit()
    queue_id = cursor.lastrowid
    release_dir = local_dir / "testfolder10"
    release_dir.mkdir()

    item_id = await _make_item_row(db, queue_id, "testfolder10", is_dir=True, remote_size=2000)
    await _make_item_row(db, queue_id, "testfolder10/a.mkv", is_dir=False, remote_size=1000)
    await _make_item_row(db, queue_id, "testfolder10/b.mkv", is_dir=False, remote_size=1000)
    await db.commit()
    (release_dir / "a.mkv").write_bytes(b"a" * 1000)
    (release_dir / "b.mkv.lftp").write_bytes(b"b" * 500)

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=43, item_id=item_id, kind="mirror")
    proc = _mirror_proc(
        job_id=43,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="testfolder10",
        local_root=release_dir,
        bytes_total=2000,
        tmp_path=tmp_path,
    )
    q._running[43] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "PARTIAL"
    # The whole point: postprocess.trigger() is the only door to the move-mode remote delete
    # (`core/postprocess.py`'s verify -> delete -> extract -> move pipeline). Never called here
    # means the delete gate was never reached, let alone passed.
    assert q.postprocess.triggered == []
