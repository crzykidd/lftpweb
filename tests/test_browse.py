"""`core/browse.py` (resolution) and `api/browse.py` (HTTP wrapper) -- the Settings -> Queues
path-browse dialog (DESIGN.md §9.2, GitHub issue #4,
`prompts/done/2026-08-16-path-browse-dialog.md`).

Local resolution is tested against real trees under `tmp_path` -- no mocking needed, the whole
point is real `os.scandir` behavior. Remote resolution is tested two ways: a fake in-memory SFTP
client (fast, no docker, exercises the walk-up/symlink/truncation logic exhaustively) plus a
handful of live checks against the fake seedbox (`docker-compose.test.yml`, same
`SEEDBOX_HOST`/`_seedbox_reachable` convention as `tests/test_remote.py`) proving the real
`asyncssh.SFTPClient` actually satisfies what the fake assumes -- in particular that `.type` is
populated on `scandir`/`stat` results for an SFTPv3 server (verified directly against asyncssh
2.24.0's source before writing this; see `core/browse.py`'s own comments).
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh
import pytest
from fastapi.testclient import TestClient

from lftpweb.core import browse
from lftpweb.main import app

# --- Local resolution (real filesystem, tmp_path) -------------------------------------------


def test_resolve_local_dir_exact_path(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")

    result = browse.resolve_local_dir(str(tmp_path))

    assert result.path == str(tmp_path)
    assert result.fallback_from is None
    assert [e.name for e in result.entries] == ["sub"]  # the file is never listed, only dirs
    assert not result.truncated


def test_resolve_local_dir_parent_is_none_at_root():
    result = browse.resolve_local_dir("/")
    assert result.path == "/"
    assert result.parent is None


def test_resolve_local_dir_parent_is_the_actual_parent(tmp_path):
    (tmp_path / "sub").mkdir()
    result = browse.resolve_local_dir(str(tmp_path / "sub"))
    assert result.parent == str(tmp_path)


def test_resolve_local_dir_walks_up_on_nonexistent_tail(tmp_path):
    result = browse.resolve_local_dir(str(tmp_path / "does-not-exist" / "nor-this"))
    assert result.path == str(tmp_path)
    assert result.fallback_from == str(tmp_path / "does-not-exist" / "nor-this")


def test_resolve_local_dir_walks_up_when_the_path_is_a_file_not_a_directory(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    result = browse.resolve_local_dir(str(tmp_path / "file.txt"))
    assert result.path == str(tmp_path)
    assert result.fallback_from == str(tmp_path / "file.txt")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_resolve_local_dir_walks_up_on_permission_denied(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        result = browse.resolve_local_dir(str(blocked))
    finally:
        blocked.chmod(0o755)  # so pytest can clean up tmp_path afterwards
    assert result.path == str(tmp_path)
    assert result.fallback_from == str(blocked)


def test_resolve_local_dir_non_absolute_falls_back_to_root_with_no_note():
    result = browse.resolve_local_dir("relative/path")
    assert result.path == "/"
    assert result.fallback_from is None


def test_resolve_local_dir_empty_falls_back_to_root():
    result = browse.resolve_local_dir("")
    assert result.path == "/"
    assert result.fallback_from is None
    assert browse.resolve_local_dir(None).path == "/"


def test_resolve_local_dir_tilde_is_meaningless_here_and_falls_back_to_root():
    # DESIGN.md §9.2 / the prompt's own wording: the container's app user has no real home.
    result = browse.resolve_local_dir("~/downloads")
    assert result.path == "/"
    assert result.fallback_from is None


def test_resolve_local_dir_normalizes_dot_dot_without_a_fallback_note(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    weird = str(tmp_path / "a" / "b" / ".." / "..")  # normalizes straight to tmp_path
    result = browse.resolve_local_dir(weird)
    assert result.path == str(tmp_path)
    assert result.fallback_from is None  # normalization isn't a failure-driven walk-up


def test_resolve_local_dir_symlink_to_directory_counts_as_a_directory(tmp_path):
    (tmp_path / "real_dir").mkdir()
    (tmp_path / "link_to_dir").symlink_to(tmp_path / "real_dir")
    (tmp_path / "link_to_file").symlink_to(tmp_path / "nonexistent_file")  # broken link

    result = browse.resolve_local_dir(str(tmp_path))
    names = {e.name for e in result.entries}
    assert "link_to_dir" in names
    assert "link_to_file" not in names  # broken symlink is never a directory


def test_resolve_local_dir_entries_sorted_by_name(tmp_path):
    for name in ("zebra", "apple", "mango"):
        (tmp_path / name).mkdir()
    result = browse.resolve_local_dir(str(tmp_path))
    assert [e.name for e in result.entries] == ["apple", "mango", "zebra"]


def test_resolve_local_dir_truncates_and_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr(browse, "MAX_ENTRIES", 3)
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
    result = browse.resolve_local_dir(str(tmp_path))
    assert len(result.entries) == 3
    assert result.truncated is True


def test_resolve_local_dir_root_unlistable_raises(monkeypatch):
    monkeypatch.setattr(browse, "_try_list_local", lambda path: None)
    with pytest.raises(browse.LocalRootUnlistableError):
        browse.resolve_local_dir("/anything")
    with pytest.raises(browse.LocalRootUnlistableError):
        browse.resolve_local_dir("")


# --- Local save-time validation (`local_directory_error`) -----------------------------------


def test_local_directory_error_none_for_a_real_readable_directory(tmp_path):
    assert browse.local_directory_error(str(tmp_path)) is None


def test_local_directory_error_missing():
    error = browse.local_directory_error("/definitely/does/not/exist/anywhere")
    assert "does not exist" in error


def test_local_directory_error_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    error = browse.local_directory_error(str(f))
    assert "not a directory" in error


def test_local_directory_error_non_absolute():
    error = browse.local_directory_error("relative/path")
    assert "absolute" in error


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_local_directory_error_permission_denied(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        error = browse.local_directory_error(str(blocked))
    finally:
        blocked.chmod(0o755)
    assert error is not None


def test_local_directory_error_never_creates_the_directory(tmp_path):
    target = tmp_path / "never-created"
    browse.local_directory_error(str(target))
    assert not target.exists()


# --- Remote resolution (fake in-memory SFTP client) ------------------------------------------


@dataclass
class _FakeAttrs:
    type: int


@dataclass
class _FakeEntry:
    filename: str
    attrs: _FakeAttrs


@dataclass
class _FakeSFTP:
    """Duck-types just the three `asyncssh.SFTPClient` methods `core/browse.py` calls --
    `scandir`, `stat`, `realpath` -- against an in-memory tree, so the walk-up/symlink/
    truncation logic is testable without a live SSH connection at all.
    """

    tree: dict[str, list[_FakeEntry]]  # path -> its listing; absent key = unlistable
    home: str = "/home/seeduser"
    realpath_overrides: dict[str, str] = field(default_factory=dict)
    # A symlink's own path (as `core/browse.py._posix_join` would build it) -> what `stat`
    # (which follows symlinks) reports for its target -- what a real SFTP server's `stat`
    # would tell us without this test standing up an actual symlink.
    stat_overrides: dict[str, _FakeAttrs] = field(default_factory=dict)

    async def scandir(self, path: str):
        if path not in self.tree:
            raise asyncssh.SFTPNoSuchFile(f"no such directory: {path}")
        for entry in self.tree[path]:
            yield entry

    async def stat(self, path: str) -> _FakeAttrs:
        if path in self.stat_overrides:
            return self.stat_overrides[path]
        if path in self.tree:
            return _FakeAttrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY)
        # A plain file that isn't itself a directory key in `tree`.
        if path in getattr(self, "_files", ()):
            return _FakeAttrs(type=asyncssh.FILEXFER_TYPE_REGULAR)
        raise asyncssh.SFTPNoSuchFile(f"no such file: {path}")

    async def realpath(self, path: str) -> str:
        if path == ".":
            return self.home
        if path in self.realpath_overrides:
            return self.realpath_overrides[path]
        return path if path.startswith("/") else f"{self.home}/{path}"


async def test_resolve_remote_dir_exact_path():
    sftp = _FakeSFTP(
        tree={
            "/data/pickup": [
                _FakeEntry("Release.One", _FakeAttrs(asyncssh.FILEXFER_TYPE_DIRECTORY)),
                _FakeEntry("loose.txt", _FakeAttrs(asyncssh.FILEXFER_TYPE_REGULAR)),
            ]
        }
    )
    result = await browse.resolve_remote_dir(sftp, "/data/pickup")
    assert result.path == "/data/pickup"
    assert result.fallback_from is None
    assert [e.name for e in result.entries] == ["Release.One"]


async def test_resolve_remote_dir_walks_up_on_missing_directory():
    sftp = _FakeSFTP(
        tree={"/data": [_FakeEntry("pickup", _FakeAttrs(asyncssh.FILEXFER_TYPE_DIRECTORY))]}
    )
    result = await browse.resolve_remote_dir(sftp, "/data/pickup/gone")
    assert result.path == "/data"
    assert result.fallback_from == "/data/pickup/gone"


async def test_resolve_remote_dir_empty_resolves_to_home():
    sftp = _FakeSFTP(tree={"/home/seeduser": []}, home="/home/seeduser")
    result = await browse.resolve_remote_dir(sftp, "")
    assert result.path == "/home/seeduser"
    assert result.fallback_from is None


async def test_resolve_remote_dir_tilde_resolves_against_home():
    sftp = _FakeSFTP(
        tree={"/home/seeduser/downloads": []},
        home="/home/seeduser",
        realpath_overrides={"~/downloads": "/home/seeduser/downloads"},
    )
    result = await browse.resolve_remote_dir(sftp, "~/downloads")
    assert result.path == "/home/seeduser/downloads"
    assert result.fallback_from is None


async def test_resolve_remote_dir_half_typed_tilde_path_walks_up_to_the_existing_ancestor():
    # The prompt's own worked example: "~/downloads/rtor" opens at "~/downloads" via walk-up.
    sftp = _FakeSFTP(
        tree={"/home/seeduser/downloads": []},
        home="/home/seeduser",
        realpath_overrides={"~/downloads/rtor": "/home/seeduser/downloads/rtor"},
    )
    result = await browse.resolve_remote_dir(sftp, "~/downloads/rtor")
    assert result.path == "/home/seeduser/downloads"
    assert result.fallback_from == "~/downloads/rtor"


async def test_resolve_remote_dir_symlink_to_directory_counts_as_a_directory():
    sftp = _FakeSFTP(
        tree={
            "/data": [
                _FakeEntry("link", _FakeAttrs(asyncssh.FILEXFER_TYPE_SYMLINK)),
                _FakeEntry("real", _FakeAttrs(asyncssh.FILEXFER_TYPE_DIRECTORY)),
            ],
            "/data/real": [],
        },
        stat_overrides={"/data/link": _FakeAttrs(asyncssh.FILEXFER_TYPE_DIRECTORY)},
    )
    result = await browse.resolve_remote_dir(sftp, "/data")
    assert {e.name for e in result.entries} == {"link", "real"}


async def test_resolve_remote_dir_broken_symlink_is_not_a_directory():
    sftp = _FakeSFTP(
        tree={"/data": [_FakeEntry("dangling", _FakeAttrs(asyncssh.FILEXFER_TYPE_SYMLINK))]}
    )
    result = await browse.resolve_remote_dir(sftp, "/data")
    assert result.entries == []


async def test_resolve_remote_dir_truncates_and_reports_it(monkeypatch):
    monkeypatch.setattr(browse, "MAX_ENTRIES", 2)
    sftp = _FakeSFTP(
        tree={
            "/data": [
                _FakeEntry(f"d{i}", _FakeAttrs(asyncssh.FILEXFER_TYPE_DIRECTORY)) for i in range(4)
            ]
        }
    )
    result = await browse.resolve_remote_dir(sftp, "/data")
    assert len(result.entries) == 2
    assert result.truncated is True


async def test_resolve_remote_dir_raises_when_nothing_can_be_listed():
    sftp = _FakeSFTP(tree={})  # even home and / are unlistable
    with pytest.raises(browse.RemoteBrowseError):
        await browse.resolve_remote_dir(sftp, "/data/pickup")


# --- Remote save-time validation (`remote_directory_error`) ---------------------------------


async def test_remote_directory_error_none_for_a_real_directory():
    sftp = _FakeSFTP(tree={"/data/pickup": []})
    assert await browse.remote_directory_error(sftp, "/data/pickup") is None


async def test_remote_directory_error_raises_for_a_missing_path():
    sftp = _FakeSFTP(tree={})
    with pytest.raises(browse.RemotePathNotFoundError):
        await browse.remote_directory_error(sftp, "/data/nope")


async def test_remote_directory_error_raises_for_a_file_not_a_directory():
    sftp = _FakeSFTP(tree={})
    sftp._files = ("/data/file.txt",)  # noqa: SLF001 - test-only injection into the fake
    with pytest.raises(browse.RemotePathNotFoundError):
        await browse.remote_directory_error(sftp, "/data/file.txt")


async def test_remote_directory_error_propagates_ambiguous_failures_as_themselves():
    class _DenyingSFTP:
        async def stat(self, path):
            raise asyncssh.SFTPPermissionDenied("nope")

    with pytest.raises(asyncssh.SFTPPermissionDenied):
        await browse.remote_directory_error(_DenyingSFTP(), "/data/pickup")


# --- Live checks against the fake seedbox (docker-compose.test.yml) -------------------------
# Same convention as tests/test_remote.py -- skipped automatically if unreachable.

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark_live = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- "
    "`docker compose -f docker-compose.test.yml up --build -d`",
)


@pytestmark_live
async def test_live_resolve_remote_dir_against_the_real_seedbox():
    from lftpweb.core.remote import HostConfig, RemoteConnectionPool

    host = HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=SEEDBOX_PASSWORD,
        known_hosts_policy="insecure",
    )
    pool = RemoteConnectionPool(known_hosts_dir=Path("/tmp"))
    try:
        conn = await pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            result = await browse.resolve_remote_dir(sftp, "/data/pickup")
            assert "Some.Release.S01E01.720p.WEB" in {e.name for e in result.entries}
            assert not any(e.name == "loose-notes.txt" for e in result.entries)  # a file, skipped

            # `~`/empty resolve against the SSH home -- confirms `.type` really is populated
            # for this server's SFTPv3 responses (core/browse.py's own comment on why that's
            # not just assumed).
            home_result = await browse.resolve_remote_dir(sftp, "")
            assert home_result.path  # some real absolute home path came back

            assert await browse.remote_directory_error(sftp, "/data/pickup") is None
            with pytest.raises(browse.RemotePathNotFoundError):
                await browse.remote_directory_error(sftp, "/data/pickup/does-not-exist-at-all")
    finally:
        await pool.close()


# --- API layer (TestClient) ------------------------------------------------------------------


def test_api_browse_local_lists_root_by_default(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/browse/local")
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "/"
        assert body["parent"] is None


def test_api_browse_local_rejects_overlong_path(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/browse/local", params={"path": "/" + ("a" * 5000)})
        assert resp.status_code == 400


def test_api_browse_local_walks_up_and_reports_fallback_from(isolated_config, tmp_path):
    with TestClient(app) as client:
        resp = client.get("/api/browse/local", params={"path": str(tmp_path / "nope")})
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == str(tmp_path)
        assert body["fallback_from"] == str(tmp_path / "nope")


def test_api_browse_remote_409s_with_no_host_configured(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/browse/remote")
        assert resp.status_code == 409


@pytestmark_live
def test_api_browse_remote_lists_the_real_seedbox_end_to_end(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": SEEDBOX_HOST,
                "port": SEEDBOX_PORT,
                "username": SEEDBOX_USER,
                "auth_method": "password",
                "password": SEEDBOX_PASSWORD,
                "known_hosts_policy": "insecure",
            },
        )
        assert resp.status_code == 200, resp.text

        resp = client.get("/api/browse/remote", params={"path": "/data/pickup"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["path"] == "/data/pickup"
        names = {e["name"] for e in body["entries"]}
        assert "Some.Release.S01E01.720p.WEB" in names
