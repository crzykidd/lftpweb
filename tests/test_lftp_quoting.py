"""lftp script quoting, proven against the **real `lftp` binary** (security audit F1, batch C1).

Why this file exists: `_lftp_quote` used to quote POSIX-shell style (`'` -> `'\\''`) and
`tests/test_lftp.py` asserted only the resulting *string*. lftp's own tokenizer is not a POSIX
shell -- inside single quotes a backslash is an escape, and a newline ends the command even
inside quotes -- so a remote filename could end the quoted word early and run a `!` shell
command. A string assertion cannot see that; only lftp itself can say what it parsed. So these
tests run the generated command through lftp (over `file:` URLs, no network, no seedbox) and
check what actually landed on disk.

**This file must never skip.** A security regression test that silently skips in CI is worse
than none, so a missing `lftp` binary is a hard failure here (the CI test job installs lftp).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import aiosqlite
import pytest

from lftpweb.core import lftp as lftp_module
from lftpweb.core.lftp import (
    HostCreds,
    UnsafeLftpValueError,
    _lftp_quote,
    build_rc_text,
    build_transfer_command,
)
from lftpweb.core.events import EventBus
from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.core.remote import HostConfig
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate


@pytest.fixture(scope="module", autouse=True)
def _require_lftp():
    # Fail, don't skip -- see the module docstring.
    assert shutil.which("lftp"), (
        "the lftp binary is required: this is a security regression test and must not skip "
        "(apt-get install lftp)"
    )


# Names that must each survive as exactly one literal argument. `MARKER` is created *relative
# to the lftp process's cwd* by the payload names below, so a leaked `!` command shows up as a
# file in the scratch cwd.
ADVERSARIAL_NAMES = [
    "plain.mkv",
    "it's",
    "a\\b",
    "end\\",
    "\\",
    "a\\\\b",
    "x\\'; !touch MARKER #",
    "x'; !touch MARKER #",
    "x\\\\'; !touch MARKER #",
    'a"b',
    "$HOME",
    "`id`",
    "a;b",
    "a#b",
    "a&b",
    "a|b",
    "!touch MARKER",
    "*",
    "?",
    "[ab]",
    "{a,b}",
    "a b",
    "a\tb",
    "a\x0bb",
    "a\x0cb",
    "é日本語",
    "-x",
    "'",
    "''",
]


def _scratch(tmp_path: Path) -> tuple[Path, Path, Path]:
    src, dst, cwd = tmp_path / "src", tmp_path / "dst", tmp_path / "cwd"
    for d in (src, dst, cwd):
        d.mkdir()
    # Decoys a glob-expanding command would also pick up for `*`, `?`, `[ab]`, `{a,b}`.
    for decoy in ("a", "b", "zz", "DECOY1"):
        (src / decoy).write_text("decoy")
    return src, dst, cwd


def _run_lftp(script: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["lftp", "-c", script], cwd=cwd, capture_output=True, timeout=30)


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES, ids=repr)
def test_pget_round_trips_adversarial_filename_as_one_argument(tmp_path, name):
    src, dst, cwd = _scratch(tmp_path)
    (src / name).write_text("payload")
    cmd = build_transfer_command(
        "pget", str(src / name), str(dst / name), parallel=1, pget_n=1, exclude_globs=()
    )
    result = _run_lftp(f"open file:{_lftp_quote(str(src))}; {cmd}", cwd)

    assert not (cwd / "MARKER").exists(), "a `!` command embedded in a filename was executed"
    assert result.returncode == 0, result.stderr
    # Exactly one file, named exactly the original, with the right bytes: one argument for the
    # source, one for `-o`, no glob fan-out, nothing swallowed.
    assert [p.name for p in dst.iterdir()] == [name]
    assert (dst / name).read_text() == "payload"
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES, ids=repr)
def test_mirror_round_trips_adversarial_directory_name(tmp_path, name):
    src, dst, cwd = _scratch(tmp_path)
    (src / name).mkdir()
    (src / name / "inner.bin").write_text("payload")
    cmd = build_transfer_command(
        "mirror", str(src / name), str(dst), parallel=1, pget_n=1, exclude_globs=()
    )
    result = _run_lftp(f"open file:{_lftp_quote(str(src))}; {cmd}", cwd)

    assert not (cwd / "MARKER").exists(), "a `!` command embedded in a directory name ran"
    assert result.returncode == 0, result.stderr
    assert [p.name for p in dst.iterdir()] == [name]
    assert (dst / name / "inner.bin").read_text() == "payload"
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("glob", ADVERSARIAL_NAMES, ids=repr)
def test_exclude_glob_arguments_cannot_run_commands(tmp_path, glob):
    # `--exclude-glob` takes pattern text (patterns.py), which is user-authored and reaches
    # lftp quoted by the same function.
    src, dst, cwd = _scratch(tmp_path)
    (src / "rel").mkdir()
    (src / "rel" / "f.bin").write_text("x")
    cmd = build_transfer_command(
        "mirror", str(src / "rel"), str(dst), parallel=1, pget_n=1, exclude_globs=(glob,)
    )
    result = _run_lftp(f"open file:{_lftp_quote(str(src))}; {cmd}", cwd)
    assert not (cwd / "MARKER").exists()
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b", "x\n!touch MARKER #"])
def test_unquotable_values_are_refused_not_escaped(value):
    # lftp ends the command at a newline even inside quotes and mangles CR; NUL cannot be in
    # an argv at all. There is no safe spelling, so these are refused outright.
    with pytest.raises(UnsafeLftpValueError):
        _lftp_quote(value)


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b"])
@pytest.mark.parametrize("kind", ["pget", "mirror"])
@pytest.mark.parametrize("which", ["remote", "local"])
def test_build_transfer_command_refuses_unsafe_paths(bad, kind, which):
    remote = f"/r/{bad}" if which == "remote" else "/r/ok"
    local = f"/l/{bad}" if which == "local" else "/l/ok"
    with pytest.raises(UnsafeLftpValueError):
        build_transfer_command(kind, remote, local, parallel=1, pget_n=1, exclude_globs=())


def _creds(**over) -> HostCreds:
    base = dict(
        address="seedbox.example",
        port=22,
        username="u",
        auth_method="password",
        key_path=None,
        password="p",
        known_hosts_policy="insecure",
        pinned_host_key=None,
    )
    base.update(over)
    return HostCreds(**base)


def _rc(creds: HostCreds, known_hosts: Path | None = None) -> str:
    return build_rc_text(
        creds,
        known_hosts,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )


@pytest.mark.parametrize("address", ["-oProxyCommand=touch", "a b", "a;b", "h\nopen x", "h'x", ""])
def test_rc_refuses_a_host_address_that_could_alter_the_command(address):
    # A leading `-` would reach ssh as an *option* (`-oProxyCommand=...` runs a command).
    with pytest.raises(ValueError):
        _rc(_creds(address=address))


def test_rc_refuses_a_credential_with_a_newline():
    with pytest.raises(UnsafeLftpValueError):
        _rc(_creds(password="a\nb"))
    with pytest.raises(UnsafeLftpValueError):
        _rc(_creds(username="a\nb"))


def test_connect_program_round_trips_awkward_paths_to_ssh(tmp_path):
    # lftp splits `sftp:connect-program` into argv itself (no shell is involved). A key path
    # with a space, quotes, `$`, `;` or a backslash must still arrive as ONE ssh argument.
    log = tmp_path / "args.log"
    fake_ssh = tmp_path / "fakessh"
    fake_ssh.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf "[%s]\\n" "$a" >> "{log}"; done\nexit 1\n'
    )
    fake_ssh.chmod(0o755)
    # Key path under the real `ssh` name is replaced by pointing the program at the fake: the
    # rc's program line is `ssh -a -x -i <key> ...`, so build the rc and swap the binary.
    key = "/tmp/a b/k'ey\"x\\y;$HOME`id`"
    rc = _rc(_creds(auth_method="key", key_path=key, password=None))
    rc = rc.replace('set sftp:connect-program "ssh ', f'set sftp:connect-program "{fake_ssh} ')
    rc_path = tmp_path / "t.rc"
    rc_path.write_text(rc + "ls;\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _run_lftp(f"set net:max-retries 1; source {_lftp_quote(str(rc_path))}", cwd)
    args = log.read_text().split("\n")
    assert f"[{key}]" in args
    assert list(cwd.iterdir()) == []


# --- queue integration: refused before spawn, named error, never retried ----------------------


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


async def _seed(db, tmp_path: Path, rel_path: str, *, is_dir: bool) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', '127.0.0.1', 2222, 'u', 'password', 'insecure')"
    )
    host_id = cursor.lastrowid
    local = tmp_path / "local"
    local.mkdir()
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'test', '/data/pickup', ?, 1, 'copy')",
        (host_id, str(local)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, ?, 1000, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path, 1 if is_dir else 0),
    )
    await db.commit()
    return cursor.lastrowid


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b"])
@pytest.mark.parametrize("is_dir", [False, True])
async def test_unsafe_name_fails_the_job_before_spawn_and_is_never_retried(
    db, tmp_path, monkeypatch, bad, is_dir
):
    item_id = await _seed(db, tmp_path, f"Show{bad}!touch MARKER #", is_dir=is_dir)
    await save_transfer_settings(
        db,
        TransferSettings(
            max_bandwidth_bps=10_000_000,
            max_concurrent_transfers=2,
            small_item_threshold_bytes=0,
            small_lane_reserve_bps=0,
            min_share_floor_bps=0,
            mirror_parallel_transfer_count=1,
            mirror_use_pget_n=1,
            pget_default_n=1,
        ),
    )

    async def host_provider():
        return HostConfig(
            id=1,
            address="127.0.0.1",
            port=2222,
            username="u",
            auth_method="password",
            password="p",
            known_hosts_policy="insecure",
        )

    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=host_provider,
    )
    spawned: list[object] = []

    async def _must_not_spawn(spec, **kw):
        spawned.append(spec)
        raise AssertionError("an unsafe name must be refused before lftp is ever spawned")

    monkeypatch.setattr(lftp_module, "spawn", _must_not_spawn)
    job_id = await q.enqueue_item(item_id)
    await q.tick()

    assert spawned == []
    job = await (
        await db.execute("SELECT state, error_class, output_tail FROM job WHERE id = ?", (job_id,))
    ).fetchone()
    assert job["state"] == "failed"
    assert job["error_class"] == "UNSAFE_PATH"
    assert job["output_tail"]  # a human-readable reason, not blank
    item = await (
        await db.execute(
            "SELECT state, error_class, auto_queue_suppressed FROM item WHERE id = ?", (item_id,)
        )
    ).fetchone()
    assert item["state"] == "FAILED"
    assert item["error_class"] == "UNSAFE_PATH"
    assert item["auto_queue_suppressed"] == 1  # auto-queue must not retry it forever
    # Nothing was created locally for it either (the parent mkdir happens after the guard).
    assert [p.name for p in (tmp_path / "local").iterdir()] == []
