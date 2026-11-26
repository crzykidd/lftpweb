"""Hostile filenames through a real `pget` / `mirror` over real ssh + sftp (security audit F1,
batch C1) -- the seedbox-backed counterpart of `tests/test_lftp_quoting.py`.

Runs `core/lftp.py.spawn` itself (so the rc file, the quoted `open` line and the transfer
command are exactly what production builds) against the fake seedbox, with files and
directories whose *names* would, under the old POSIX-style quoting, end the quoted word and run
a `!` shell command. Asserts the bytes land at the right local path and that no command ran.
Skipped only if the seedbox is unreachable -- the same reason string `tests/test_queue.py` uses,
which CI's skip-guard step turns into a hard failure.
"""

from __future__ import annotations

import socket
from uuid import uuid4

import pytest

from lftpweb.core import lftp
from lftpweb.core.remote import HostConfig, RemoteConnectionPool

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- `docker compose -f docker-compose.test.yml up --build -d`",
)

# `MARKER` is created relative to lftp's cwd (the test chdirs into an empty scratch dir) if a
# `!` command in the name ever runs.
NAMES = [
    "it's",
    "a\\b",
    "end\\",
    "x\\'; !touch MARKER #",
    "x'; !touch MARKER #",
    "star*",
    "q?mark",
    "[ab]",
    'a"b $HOME `id`',
]


def _host() -> HostConfig:
    return HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username="seeduser",
        auth_method="password",
        password="testpass123",
        known_hosts_policy="insecure",
    )


def _creds() -> lftp.HostCreds:
    h = _host()
    return lftp.HostCreds(
        address=h.address,
        port=h.port,
        username=h.username,
        auth_method="password",
        key_path=None,
        password=h.password,
        known_hosts_policy="insecure",
        pinned_host_key=None,
    )


async def _upload(tmp_path, remote_dir: str, files: dict[str, bytes], dirs: dict[str, bytes]):
    pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    conn = await pool.get_connection(_host())
    async with conn.start_sftp_client() as sftp:
        await sftp.makedirs(remote_dir, exist_ok=True)
        # Decoys that glob expansion of `star*`, `q?mark`, `[ab]` would also match.
        for decoy in ("starX", "qXmark", "a", "b"):
            async with sftp.open(f"{remote_dir}/{decoy}", "wb") as f:
                await f.write(b"decoy")
        for name, data in files.items():
            async with sftp.open(f"{remote_dir}/{name}", "wb") as f:
                await f.write(data)
        for name, data in dirs.items():
            await sftp.makedirs(f"{remote_dir}/{name}", exist_ok=True)
            async with sftp.open(f"{remote_dir}/{name}/inner.bin", "wb") as f:
                await f.write(data)


async def _run(tmp_path, kind: str, remote_path: str, local_path: str) -> tuple[int, str]:
    run_dir = tmp_path / "run"
    spec = lftp.JobSpec(
        job_id=abs(hash((kind, remote_path))) % 10_000_000,
        kind=kind,  # type: ignore[arg-type]
        creds=_creds(),
        remote_path=remote_path,
        local_path=local_path,
        run_dir=str(run_dir),
        mirror_rename_target=False,
    )
    job = await lftp.spawn(spec)
    try:
        return await lftp.wait_and_capture(job)
    finally:
        job.cleanup()


@pytest.mark.parametrize("name", NAMES, ids=repr)
async def test_real_pget_of_a_hostile_filename_lands_intact_and_runs_nothing(
    tmp_path, monkeypatch, name
):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    remote_dir = f"/data/pickup/quote-e2e-{uuid4().hex[:12]}"
    payload = f"payload for {name}".encode()
    await _upload(tmp_path, remote_dir, {name: payload}, {})

    local = tmp_path / "local"
    local.mkdir()
    code, output = await _run(tmp_path, "pget", f"{remote_dir}/{name}", str(local / name))

    assert code == 0, output
    assert [p.name for p in local.iterdir()] == [name]
    assert (local / name).read_bytes() == payload
    assert list(cwd.iterdir()) == [], "a `!` command in a seedbox filename executed"


@pytest.mark.parametrize("name", NAMES, ids=repr)
async def test_real_mirror_of_a_hostile_directory_lands_intact_and_runs_nothing(
    tmp_path, monkeypatch, name
):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    remote_dir = f"/data/pickup/quote-e2e-{uuid4().hex[:12]}"
    payload = f"inner of {name}".encode()
    await _upload(tmp_path, remote_dir, {}, {name: payload})

    local = tmp_path / "local"
    local.mkdir()
    # mirror's target is the PARENT directory (core/lftp.py.build_transfer_command docstring).
    code, output = await _run(tmp_path, "mirror", f"{remote_dir}/{name}", str(local))

    assert code == 0, output
    assert [p.name for p in local.iterdir()] == [name]
    assert (local / name / "inner.bin").read_bytes() == payload
    assert list(cwd.iterdir()) == [], "a `!` command in a seedbox directory name executed"
