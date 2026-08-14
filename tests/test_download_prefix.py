"""Unit coverage for "folder prefix during transfer" (2026-08-14,
`prompts/2026-08-14-in-flight-folder-prefix.md`, `core/download_prefix.py`) -- the pieces that
don't need a live seedbox: prefix resolution and validation, the `mirror`-target argv change,
`core/local_scan.py`'s configurable filter, `core/engine.py.Engine._active_download_prefixes`'s
DB-backed prefix set, and `core/queue.py._reap_one`'s rename-before-DOWNLOADED step. The full
pipeline against the real fake seedbox is `tests/test_download_prefix_e2e.py`.

No seedbox needed anywhere in this file -- `_reap_one` is exercised directly against a hand-built
`_RunningProcess` whose `wait_task` is an already-resolved `asyncio.Task`, exactly
`tests/test_queue_completeness.py`'s own shape (that file's module docstring explains why this is
safe: nothing here spawns a real lftp process).
"""

from __future__ import annotations

import pytest

from lftpweb.core import download_prefix, lftp, local_scan
from lftpweb.core.engine import Engine, QueueConfig
from lftpweb.core.events import EventBus
from lftpweb.core.queue import _RunningProcess
from lftpweb.core.settle import SettleSettings, save_settle_settings
from test_queue import _make_db, _make_host_row, _make_item_row, _make_queue_row, _queue_for
from test_queue_completeness import (
    _event_rows,
    _fake_spawned,
    _FakePostprocess,
    _item_row,
    _make_job_row,
    _resolved_wait_task,
)

# --- resolve_for_queue / validate_prefix (pure functions) -------------------------------------


def test_resolve_for_queue_both_null_inherits_site():
    enabled, prefix = download_prefix.resolve_for_queue(
        None, None, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".dl-")
    )
    assert (enabled, prefix) == (True, ".dl-")


def test_resolve_for_queue_overrides_are_independent_not_anded():
    """`3500b3f`'s shape, not the AND-of-two-toggles it replaced: a queue can override just the
    toggle while inheriting the site's prefix string, or vice versa.
    """
    site = download_prefix.DownloadPrefixSettings(enabled=False, prefix=".downloading-")
    # Queue overrides only `enabled` -> True; prefix still inherits the site string.
    assert download_prefix.resolve_for_queue(True, None, site) == (True, ".downloading-")
    # Queue overrides only the prefix string; `enabled` still inherits the site's False.
    assert download_prefix.resolve_for_queue(None, ".custom-", site) == (False, ".custom-")
    # Both overridden.
    assert download_prefix.resolve_for_queue(True, ".custom-", site) == (True, ".custom-")


@pytest.mark.parametrize(
    "prefix,enabled,expect_error",
    [
        ("", False, False),  # blank + disabled: inert, no error
        ("", True, True),  # blank + enabled: must not be empty
        (".downloading-", True, False),  # the shipped default
        ("a/b", True, True),  # path separator
        ("a\\b", True, True),  # path separator (backslash)
        (".", True, True),
        ("..", True, True),
        ("_UNPACK_", True, True),  # exact collision
        ("_UNPACK_x", True, True),  # candidate starts with a reserved name
        ("_UN", True, True),  # reserved name starts with the candidate
        (".lftpweb-mount-ok", True, True),
        ("_FAILED_", True, True),
        # Shape checks are unconditional -- see validate_prefix's own docstring -- so a bad
        # shape is rejected even while `enabled=False` (a per-queue override saved now, taking
        # effect later without going through this function again).
        ("a/b", False, True),
    ],
)
def test_validate_prefix(prefix, enabled, expect_error):
    error = download_prefix.validate_prefix(prefix, enabled=enabled)
    assert (error is not None) == expect_error


# --- core/lftp.py.build_transfer_command's mirror_rename_target flag ---------------------------


def test_build_transfer_command_mirror_default_forces_a_trailing_slash():
    cmd = lftp.build_transfer_command(
        "mirror", "/remote/Release", "/local/parent", parallel=1, pget_n=1, exclude_globs=()
    )
    assert "'/local/parent/'" in cmd
    assert "prefix" not in cmd


def test_build_transfer_command_mirror_rename_target_passes_the_literal_path():
    cmd = lftp.build_transfer_command(
        "mirror",
        "/remote/Release",
        "/local/parent/.downloading-Release",
        parallel=1,
        pget_n=1,
        exclude_globs=(),
        mirror_rename_target=True,
    )
    assert "'/local/parent/.downloading-Release'" in cmd
    assert "'/local/parent/.downloading-Release/'" not in cmd


def test_build_transfer_command_pget_ignores_the_flag():
    # `mirror_rename_target` only ever means something for `mirror` -- a `pget` job never
    # reaches the branch that reads it (core/queue.py never sets it True for one either).
    cmd = lftp.build_transfer_command(
        "pget",
        "/remote/file.mkv",
        "/local/file.mkv",
        parallel=1,
        pget_n=4,
        exclude_globs=(),
        mirror_rename_target=True,
    )
    assert cmd.startswith("pget -c -n 4")


# --- core/local_scan.py.scan_local's extra_dir_prefixes ----------------------------------------


def test_scan_local_default_does_not_filter_anything_new(tmp_path):
    (tmp_path / ".downloading-Release").mkdir()
    (tmp_path / ".downloading-Release" / "a.mkv").write_bytes(b"x")
    entries = local_scan.scan_local(tmp_path)
    assert ".downloading-Release" in entries


def test_scan_local_filters_the_active_prefix_at_the_top_level(tmp_path):
    (tmp_path / ".downloading-Release").mkdir()
    (tmp_path / ".downloading-Release" / "a.mkv").write_bytes(b"x")
    (tmp_path / "Other.Release").mkdir()
    (tmp_path / "Other.Release" / "b.mkv").write_bytes(b"y")
    entries = local_scan.scan_local(tmp_path, extra_dir_prefixes=(".downloading-",))
    assert ".downloading-Release" not in entries
    assert ".downloading-Release/a.mkv" not in entries
    assert "Other.Release" in entries
    assert "Other.Release/b.mkv" in entries


def test_scan_local_filters_a_prefixed_directory_at_any_depth(tmp_path):
    """Matches UNPACK_PREFIX/FAILED_PREFIX's own "any depth" behaviour -- a directory item
    being downloaded need not be top-level (DESIGN.md phase 2: `item` rows exist per node).
    """
    (tmp_path / "parent").mkdir()
    (tmp_path / "parent" / ".downloading-Child").mkdir()
    (tmp_path / "parent" / ".downloading-Child" / "f.mkv").write_bytes(b"x")
    entries = local_scan.scan_local(tmp_path, extra_dir_prefixes=(".downloading-",))
    assert "parent" in entries
    assert "parent/.downloading-Child" not in entries
    assert "parent/.downloading-Child/f.mkv" not in entries


def test_scan_local_multiple_prefixes_stale_plus_current(tmp_path):
    """The stale-prefix case `Engine._active_download_prefixes` exists for: a job spawned under
    an old prefix is still filtered even after the active setting has moved on to a new one.
    """
    (tmp_path / ".old-Release").mkdir()
    (tmp_path / ".old-Release" / "a.mkv").write_bytes(b"x")
    (tmp_path / ".new-Other").mkdir()
    (tmp_path / ".new-Other" / "b.mkv").write_bytes(b"y")
    entries = local_scan.scan_local(tmp_path, extra_dir_prefixes=(".old-", ".new-"))
    assert entries == {}


# --- core/engine.py.Engine._active_download_prefixes -------------------------------------------


@pytest.fixture
async def db(tmp_path):
    conn = await _make_db(tmp_path)
    await save_settle_settings(conn, SettleSettings(enabled=False))
    try:
        yield conn
    finally:
        await conn.close()


def _queue_config(
    queue_id: int, *, local_path: str, download_prefix_enabled=None, download_prefix_=None
):
    return QueueConfig(
        id=queue_id,
        host_id=1,
        name="q",
        remote_path="/remote",
        local_path=local_path,
        staging_path=None,
        enabled=True,
        sync_mode="copy",
        download_prefix_enabled=download_prefix_enabled,
        download_prefix=download_prefix_,
    )


async def test_active_download_prefixes_empty_when_disabled_and_nothing_pending(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path)
    # Explicitly disabled rather than relying on the dataclass default -- that default flipped to
    # `True` on 2026-08-14 and this test is about the *disabled* path, so it has to say so.
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=False)
    )
    engine = Engine(db, str(tmp_path), events=EventBus())
    q = _queue_config(queue_id, local_path=str(tmp_path))
    prefixes = await engine._active_download_prefixes(q)
    assert prefixes == ()


async def test_active_download_prefixes_includes_the_resolved_site_prefix_when_enabled(
    db, tmp_path
):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path)
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
    engine = Engine(db, str(tmp_path), events=EventBus())
    q = _queue_config(queue_id, local_path=str(tmp_path))
    prefixes = await engine._active_download_prefixes(q)
    assert prefixes == (".downloading-",)


async def test_active_download_prefixes_unions_in_every_distinct_pending_value(db, tmp_path):
    """The stale-prefix safety net: even with the feature off site-wide right now, a scan must
    still skip a directory an earlier, differently-configured spawn is still writing into.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path)
    item_a = await _make_item_row(db, queue_id, "A", is_dir=True, remote_size=10)
    item_b = await _make_item_row(db, queue_id, "B", is_dir=True, remote_size=10)
    await db.execute("UPDATE item SET pending_download_prefix = '.old-' WHERE id = ?", (item_a,))
    await db.execute(
        "UPDATE item SET pending_download_prefix = '.also-old-' WHERE id = ?", (item_b,)
    )
    await db.commit()

    # Explicitly disabled: this asserts the *pending* values are unioned in on their own, so the
    # currently-resolved site prefix must not be in the set. Stated rather than inherited from
    # the dataclass default, which flipped to `True` on 2026-08-14.
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=False)
    )

    engine = Engine(db, str(tmp_path), events=EventBus())
    q = _queue_config(queue_id, local_path=str(tmp_path))  # site/queue toggle left off
    prefixes = await engine._active_download_prefixes(q)
    assert set(prefixes) == {".old-", ".also-old-"}


# --- core/queue.py.TransferQueue._resolve_download_prefix_for_spawn ----------------------------


async def test_resolve_download_prefix_for_spawn_prefers_the_items_own_pending_value(db, tmp_path):
    """The stale-prefix rule that makes resume safe: once an item has a recorded
    `pending_download_prefix`, a fresh spawn must reuse it verbatim rather than recomputing from
    *today's* settings -- otherwise a resume targets a brand-new (empty) directory under a
    different name instead of the partial bytes already on disk.
    """
    q = await _queue_for(db, tmp_path)

    # Site settings now say a *different* prefix -- must not matter for this item.
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".new-")
    )

    item = {"pending_download_prefix": ".old-stale-"}
    queue_row = {"download_prefix_enabled": True, "download_prefix": None}
    resolved = await q._resolve_download_prefix_for_spawn(item, queue_row)
    assert resolved == ".old-stale-"


async def test_resolve_download_prefix_for_spawn_fresh_spawn_uses_resolved_settings(db, tmp_path):
    q = await _queue_for(db, tmp_path)
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
    item = {"pending_download_prefix": None}
    queue_row = {"download_prefix_enabled": None, "download_prefix": None}  # inherit
    resolved = await q._resolve_download_prefix_for_spawn(item, queue_row)
    assert resolved == ".downloading-"


async def test_resolve_download_prefix_for_spawn_disabled_returns_none(db, tmp_path):
    q = await _queue_for(db, tmp_path)
    item = {"pending_download_prefix": None}
    queue_row = {"download_prefix_enabled": False, "download_prefix": ".downloading-"}
    resolved = await q._resolve_download_prefix_for_spawn(item, queue_row)
    assert resolved is None


# --- core/queue.py._reap_one: the rename step, directory items only ----------------------------


def _mirror_proc_with_prefix(
    *, job_id, item_id, queue_id, rel_path, local_root, bytes_total, tmp_path, download_prefix_
):
    return _RunningProcess(
        job_id=job_id,
        item_id=item_id,
        queue_id=queue_id,
        rel_path=rel_path,
        is_dir=True,
        kind="mirror",
        lane="main",
        rate_limit_bps=0,
        forced_full_rate=False,
        local_root=str(local_root),
        bytes_total=bytes_total,
        remote_mtime=None,
        spawned=_fake_spawned(tmp_path, job_id),
        wait_task=_resolved_wait_task(0, "lftp output"),
        download_prefix=download_prefix_,
    )


async def test_reap_one_renames_the_prefixed_directory_to_its_real_name_on_success(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    prefixed_dir = local_dir / ".downloading-Release"
    prefixed_dir.mkdir()
    (prefixed_dir / "a.mkv").write_bytes(b"a" * 1000)

    item_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1000)
    await _make_item_row(db, queue_id, "Release/a.mkv", is_dir=False, remote_size=1000)
    await db.execute(
        "UPDATE item SET pending_download_prefix = '.downloading-' WHERE id = ?", (item_id,)
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=1, item_id=item_id, kind="mirror")
    proc = _mirror_proc_with_prefix(
        job_id=1,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="Release",
        local_root=prefixed_dir,
        bytes_total=1000,
        tmp_path=tmp_path,
        download_prefix_=".downloading-",
    )
    q._running[1] = proc

    await q._reap_one(proc)

    final_dir = local_dir / "Release"
    assert final_dir.is_dir(), "the item must end up under its real, unprefixed name"
    assert (final_dir / "a.mkv").read_bytes() == b"a" * 1000
    assert not prefixed_dir.exists(), "the prefixed directory must be gone once renamed"

    item = await _item_row(db, item_id)
    assert item["state"] == "DOWNLOADED"

    cursor = await db.execute("SELECT pending_download_prefix FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["pending_download_prefix"] is None, "cleared once the physical rename happened"

    events = await _event_rows(db, item_id, "download_prefix_removed")
    assert len(events) == 1
    assert str(prefixed_dir) in events[0]["message"]
    assert str(final_dir) in events[0]["message"]

    assert q.postprocess.triggered == [
        item_id
    ], "post-processing must see the item at its real path, after the rename"


async def test_reap_one_rename_conflict_holds_the_item_at_partial_not_downloaded(db, tmp_path):
    """A destination collision (something already sitting under the real name) must never be
    silently clobbered -- `core/postprocess.py.move_tree`'s `merge=False` refuses it, and
    `_reap_one` downgrades to the same PARTIAL/`incomplete_on_exit_zero` handling an ordinary
    short-on-bytes transfer gets: the bytes are all still there, just under the in-flight name.
    """
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    prefixed_dir = local_dir / ".downloading-Release"
    prefixed_dir.mkdir()
    (prefixed_dir / "a.mkv").write_bytes(b"a" * 1000)
    # The real name already exists -- a genuine conflict `move_tree` must refuse.
    (local_dir / "Release").mkdir()
    (local_dir / "Release" / "old.mkv").write_bytes(b"stale")

    item_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1000)
    await db.commit()

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=2, item_id=item_id, kind="mirror")
    proc = _mirror_proc_with_prefix(
        job_id=2,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="Release",
        local_root=prefixed_dir,
        bytes_total=1000,
        tmp_path=tmp_path,
        download_prefix_=".downloading-",
    )
    q._running[2] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "PARTIAL"
    assert prefixed_dir.exists(), "the in-flight content must be preserved, not lost"
    assert (prefixed_dir / "a.mkv").read_bytes() == b"a" * 1000

    events = await _event_rows(db, item_id, "incomplete_on_exit_zero")
    assert len(events) == 1
    assert "folder-prefix rename failed" in events[0]["message"]
    assert q.postprocess.triggered == []


async def test_reap_one_pget_job_is_unaffected(db, tmp_path):
    """Scope limit: `download_prefix` is `None` for every `pget` job (`core/queue.py.
    _spawn_decision` never sets it for a single-file item) -- `_finalize_download_prefix` must
    be a pure no-op for one, exactly the existing (pre-this-task) success path.
    """
    from test_queue_completeness import _pget_proc

    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    target = local_dir / "file.mkv"
    target.write_bytes(b"x" * 500)

    item_id = await _make_item_row(db, queue_id, "file.mkv", is_dir=False, remote_size=500)
    await db.commit()

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=3, item_id=item_id, kind="pget")
    proc = _pget_proc(
        job_id=3,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="file.mkv",
        local_root=target,
        bytes_total=500,
        tmp_path=tmp_path,
    )
    assert proc.download_prefix is None
    q._running[3] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "DOWNLOADED"
    assert target.exists()


async def test_reap_one_unsettled_item_leaves_the_prefixed_directory_untouched(db, tmp_path):
    """The settle gate held (§4.7's directory-upload race) must not rename anything -- the
    release may not be whole yet, and a resumed job needs the prefixed directory to still be
    exactly where it left it.
    """
    await save_settle_settings(db, SettleSettings(enabled=True))
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    prefixed_dir = local_dir / ".downloading-Release"
    prefixed_dir.mkdir()
    (prefixed_dir / "a.mkv").write_bytes(b"a" * 1000)

    item_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1000)
    await db.commit()

    q = await _queue_for(db, tmp_path)
    q.postprocess = _FakePostprocess()
    await _make_job_row(db, job_id=4, item_id=item_id, kind="mirror")
    proc = _mirror_proc_with_prefix(
        job_id=4,
        item_id=item_id,
        queue_id=queue_id,
        rel_path="Release",  # top-level -- settle eligibility applies
        local_root=prefixed_dir,
        bytes_total=1000,
        tmp_path=tmp_path,
        download_prefix_=".downloading-",
    )
    q._running[4] = proc

    await q._reap_one(proc)

    item = await _item_row(db, item_id)
    assert item["state"] == "REMOTE_ONLY"
    assert item["substate"] == "settling"
    assert prefixed_dir.exists(), "nothing may be renamed while the item hasn't settled"
    assert q.postprocess.triggered == []


async def test_the_site_default_is_on(db):
    """The 2026-08-14 flip, pinned. An absent `setting` row must read `enabled=True` -- an
    existing install that has never opened Settings -> Transfer is exactly the case this default
    exists to protect, and a silent revert to `False` would leave that install running with the
    importer race live again.
    """
    settings = await download_prefix.load_download_prefix_settings(db)
    assert settings.enabled is True
    assert settings.prefix == download_prefix.DEFAULT_PREFIX


async def test_a_stored_row_missing_the_key_reads_the_dataclass_default(db):
    """`load_*` must not carry its own literal that can drift from the dataclass.
    `core/settle.py.load_settle_settings` has exactly that split today (absent row -> `True`,
    stored row missing the key -> `False`); not repeating it here.
    """
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) "
        "VALUES (?, '{\"prefix\": \".downloading-\"}', STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))",
        (download_prefix.SETTING_KEY,),
    )
    await db.commit()
    settings = await download_prefix.load_download_prefix_settings(db)
    assert settings.enabled is True


async def test_it_can_still_be_turned_off(db):
    """Defaulting on must not make it un-disableable."""
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=False)
    )
    settings = await download_prefix.load_download_prefix_settings(db)
    assert settings.enabled is False
