"""Audit batch C3: the "Extra lftp settings" box is no longer a shell.

It used to be appended to every job's rc file verbatim, so `!cmd`, a second command after `;`,
or a setting that runs a program executed inside the container. Now: `set <key> <value>` lines
only, exact known key names only, a deny-list of settings that run programs / write files /
leak credentials, every accepted line re-emitted through `_lftp_quote`. Checked at write time
(422); a stored value with bad lines has just those lines dropped at spawn and reported via
`/api/health` -- transfers are never held.
"""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from lftpweb.core import lftp
from lftpweb.core.events import EventBus
from lftpweb.core.lftp import (
    DENIED_LFTP_SETTINGS,
    KNOWN_LFTP_SETTINGS,
    HostCreds,
    UnsafeLftpSettingsError,
    build_rc_text,
    parse_extra_lftp_settings,
    render_extra_lftp_settings,
)
from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.db import migrate
from lftpweb.main import app

needs_lftp = pytest.mark.skipif(shutil.which("lftp") is None, reason="lftp binary not on PATH")

# Each is refused at write time (422) and can never reach an rc file.
UNSAFE = [
    pytest.param("!id", id="bang-command"),
    pytest.param("set net:timeout 30; !id", id="set-then-bang"),
    pytest.param("set net:timeout 30s && id", id="and-and"),
    pytest.param("set net:timeout '30s;id'", id="separator-inside-quoted-value"),
    pytest.param("set net:timeout 30s | id", id="pipe"),
    pytest.param("set net:timeout 30s & id", id="ampersand"),
    pytest.param("set xfer:verify-command /bin/sh", id="dangerous-key"),
    pytest.param('set sftp:connect-program "sh -c id"', id="connect-program"),
    pytest.param('set cmd:at-exit "!id"', id="event-hook"),
    pytest.param('set cmd:at-exit/sftp:host "!id"', id="event-hook-with-closure"),
    pytest.param('set cmd:at-exi "!id"', id="abbreviated-dangerous-key"),
    pytest.param("set n:tim 30", id="abbreviated-harmless-key"),
    pytest.param("set log:file /config/lftpweb.db", id="writes-arbitrary-file"),
    pytest.param("set cmd:trace yes", id="echoes-the-password"),
    pytest.param("set xfer:temp-file-name ../../config/*", id="slash-in-a-file-name-value"),
    pytest.param("set nosuch:setting 1", id="unknown-key"),
    pytest.param("source /tmp/x", id="other-command"),
    pytest.param("shell id", id="shell"),
    pytest.param("set -a", id="option-not-key"),
    pytest.param("set net:timeout 30\r!id", id="embedded-cr"),
]


def _creds() -> HostCreds:
    return HostCreds(
        address="example.invalid",
        port=22,
        username="u",
        auth_method="password",
        key_path=None,
        password="p",
        known_hosts_policy="insecure",
        pinned_host_key=None,
    )


def _rc(extra: str) -> str:
    return build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings=extra,
    )


def _transfer_body(extra: str) -> dict:
    return {
        "max_bandwidth_bps": 10_000_000,
        "max_concurrent_transfers": 2,
        "small_item_threshold_bytes": 10_000_000,
        "small_lane_concurrency": 2,
        "small_lane_reserve_bps": None,
        "min_share_floor_bps": 500_000,
        "mirror_parallel_transfer_count": 4,
        "mirror_use_pget_n": 4,
        "pget_default_n": 4,
        "max_attempts": 5,
        "retry_backoff_base_s": 30.0,
        "extra_lftp_settings": extra,
    }


# --- write time ----------------------------------------------------------------------------


@pytest.mark.parametrize("text", UNSAFE)
def test_unsafe_line_is_a_422_naming_the_line(isolated_config, text):
    body = "set net:socket-buffer 262144;\n" + text
    with TestClient(app) as client:
        resp = client.put("/api/settings/transfer", json=_transfer_body(body))
        assert resp.status_code == 422, resp.text
        assert "line 2" in resp.text
        # Nothing was stored.
        assert client.get("/api/settings/transfer").json()["extra_lftp_settings"] == ""


def test_safe_settings_save_and_read_back_as_typed(isolated_config):
    text = (
        "# tuning for the slow box\n"
        "\n"
        "set net:socket-buffer 262144;\n"
        "set sftp:max-packets-in-flight 16\n"
        'set xfer:temp-file-name "*.part";\n'
        "set net:limit-rate/sftp://seedbox.example 1000000"
    )
    with TestClient(app) as client:
        resp = client.put("/api/settings/transfer", json=_transfer_body(text))
        assert resp.status_code == 200, resp.text
        assert client.get("/api/settings/transfer").json()["extra_lftp_settings"] == text


def test_a_stored_bad_value_is_still_readable(isolated_config):
    """GET must not 500 on a value written before validation existed -- that page is where
    the user fixes it."""
    import asyncio

    from lftpweb.db import connect

    async def _store() -> None:
        db = await connect(str(isolated_config))
        await migrate(db)
        await save_transfer_settings(db, TransferSettings(extra_lftp_settings="!id"))
        await db.close()

    asyncio.run(_store())
    with TestClient(app) as client:
        resp = client.get("/api/settings/transfer")
        assert resp.status_code == 200, resp.text
        assert resp.json()["extra_lftp_settings"] == "!id"


# --- spawn time: the rc file ---------------------------------------------------------------


@pytest.mark.parametrize("text", UNSAFE)
def test_unsafe_line_never_reaches_the_rc_file(text):
    with pytest.raises(UnsafeLftpSettingsError):
        parse_extra_lftp_settings(text)
    rc = _rc("set net:socket-buffer 262144;\n" + text)
    assert "set net:socket-buffer '262144';" in rc  # the good line is still applied
    for line in rc.splitlines():
        assert line.startswith(("#", "set ", "open ")) or not line.strip(), line
    assert "!id" not in rc and "sh -c" not in rc


def test_accepted_lines_are_re_emitted_quoted_not_passed_through():
    rc = _rc(
        "# a comment\n"
        "\n"
        "  set net:socket-buffer 262144;  \n"
        'set xfer:temp-file-name "*.part"\n'
        "set cmd:default-title `touch PWNED` $(id)\n"
        "set mirror:exclude-regex\n"
    )
    assert "set net:socket-buffer '262144';" in rc
    assert "set xfer:temp-file-name '*.part';" in rc
    assert "set cmd:default-title '`touch PWNED` $(id)';" in rc
    assert "set mirror:exclude-regex;" in rc
    assert "# a comment" not in rc
    # Still after every lftpweb default and before the credential-bearing `open`.
    assert rc.index("set pget:min-chunk-size 1048576;") < rc.index("set net:socket-buffer")
    assert rc.index("set net:socket-buffer") < rc.index("open -u ")


def test_parser_contract():
    parsed = parse_extra_lftp_settings("set net:timeout 30s ;\nset net:idle 3 m")
    assert [(s.key, s.value) for s in parsed] == [("net:timeout", "30s"), ("net:idle", "3 m")]
    assert render_extra_lftp_settings("") == []
    assert lftp.extra_lftp_settings_error("set net:timeout 30") is None
    assert "line 1" in (lftp.extra_lftp_settings_error("!id") or "")


@needs_lftp
def test_real_lftp_runs_nothing_from_a_hostile_looking_but_accepted_value(tmp_path):
    """Run the real binary against the generated rc. A control proves the harness *can* see
    execution (a verbatim `!touch` does create the marker), so the pass isn't vacuous."""
    control_marker = tmp_path / "control"
    control_rc = tmp_path / "control.rc"
    control_rc.write_text(f"set net:timeout 30;\n!touch {control_marker}\n")
    subprocess.run(  # noqa: S603
        ["lftp", "-c", f"source {control_rc}"],  # noqa: S607
        capture_output=True,
        timeout=20,
        cwd=tmp_path,
    )
    assert control_marker.exists(), "control: lftp should execute a verbatim `!` line"

    marker = tmp_path / "PWNED"
    rc_path = tmp_path / "job.rc"
    rc_path.write_text(_rc(f"set cmd:default-title `touch {marker}` $(touch {marker})"))
    result = subprocess.run(  # noqa: S603
        ["lftp", "-c", f"source {rc_path}; set -a"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=20,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    titles = [ln for ln in result.stdout.splitlines() if ln.startswith("set cmd:default-title ")]
    assert titles and "touch" in titles[0], titles  # the value arrived as literal text


@needs_lftp
def test_benign_override_of_a_colliding_default_still_wins_in_real_lftp(tmp_path):
    rc = _rc("set pget:min-chunk-size 999999;")
    assert rc.index("set pget:min-chunk-size 1048576;") < rc.index(
        "set pget:min-chunk-size '999999';"
    )
    rc_path = tmp_path / "job.rc"
    rc_path.write_text(rc)
    result = subprocess.run(  # noqa: S603
        ["lftp", "-c", f"source {rc_path}; set -a"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("set pget:min-chunk-size ")]
    assert lines == ["set pget:min-chunk-size 999999"]


@needs_lftp
def test_known_settings_list_matches_the_real_binary():
    """Drift guard for `KNOWN_LFTP_SETTINGS`. The list is taken from the shipped image's lftp
    (Alpine, no torrent module, OpenSSL's `ssl:ca-path`/`ssl:crl-path`); a different build of
    the same version may differ only by those."""
    out = subprocess.run(  # noqa: S603
        ["lftp", "-c", "set -a"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    local = {ln.split()[1].split("/")[0] for ln in out.splitlines() if ln.startswith("set ")}
    build_specific = {"ssl:ca-path", "ssl:crl-path"}
    local_known = {name for name in local if not name.startswith("torrent:")}
    assert local_known - KNOWN_LFTP_SETTINGS == set()
    assert KNOWN_LFTP_SETTINGS - local - build_specific == set()
    assert set(DENIED_LFTP_SETTINGS) <= KNOWN_LFTP_SETTINGS


# --- spawn time: the queue holds instead of running -----------------------------------------


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _queued_job(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (cursor.lastrowid,),
    )
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'item', 0, 1000, 0, 'REMOTE_ONLY')",
        (cursor.lastrowid,),
    )
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, queue_position) "
        "VALUES (?, 'pget', 'queued', 'main', 0, 1, 1.0)",
        (cursor.lastrowid,),
    )
    await db.commit()
    return cursor.lastrowid


async def test_stored_bad_line_is_skipped_reported_and_never_holds(db, caplog):
    job_id = await _queued_job(db)
    spawned: list[int] = []

    async def _host():
        return SimpleNamespace(id=1, credentials_need_reentry=False)

    q = TransferQueue(db, "/config", EventBus(), host_provider=_host)

    async def _record(decision, host, settings):
        spawned.append(decision.job_id)

    q._spawn_decision = _record  # type: ignore[method-assign]

    stored = "set net:limit-rate 1000\n!id"
    await save_transfer_settings(db, TransferSettings(extra_lftp_settings=stored))
    await q._admit()
    assert spawned == [job_id]  # the job transferred despite the bad line
    assert q.lftp_settings_ignored is not None
    assert "1 extra lftp setting line(s)" in q.lftp_settings_ignored
    assert "line 2" in q.lftp_settings_ignored
    await q._admit()
    await q._admit()
    assert sum("ignoring 1 invalid extra lftp" in r.message for r in caplog.records) == 1

    # The rc for this stored value applies the good line and drops the bad one.
    rc = _rc(stored)
    assert "set net:limit-rate '1000';" in rc and "!id" not in rc

    # A different bad value logs again; a clean value clears the report.
    await save_transfer_settings(db, TransferSettings(extra_lftp_settings="!id\n!ls"))
    await q._admit()
    assert "2 extra lftp setting line(s)" in (q.lftp_settings_ignored or "")
    assert sum("ignoring" in r.message for r in caplog.records) == 2
    await save_transfer_settings(db, TransferSettings(extra_lftp_settings="set net:timeout 30"))
    await q._admit()
    assert q.lftp_settings_ignored is None


def test_health_reports_the_ignored_lines(isolated_config):
    with TestClient(app) as client:
        assert client.get("/api/health").json()["lftp_settings_ignored"] is None
        app.state.queue.lftp_settings_ignored = "1 extra lftp setting line(s) are being ignored"
        try:
            body = client.get("/api/health").json()
        finally:
            app.state.queue.lftp_settings_ignored = None
        assert "being ignored" in body["lftp_settings_ignored"]
