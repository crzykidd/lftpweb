"""Every `set` line we generate must be accepted by a real lftp binary.

This exists because of a bug that no amount of unit testing would have caught: the rc file
contained `set net:reconnect-interval-base 5s;`, which lftp rejects with

    5s: invalid unsigned number.

`net:timeout` *is* a time interval and takes `30s`, but `net:reconnect-interval-base` takes a
bare unsigned number — a distinction visible nowhere except lftp's own parser. lftp printed the
error and carried on, so the transfer ran with broken connection settings and failed with a
misleading `HOST_UNREACHABLE`, pointing the investigation at the network instead of at our own
rc file.

Asserting the rc *contains* a string only proves we wrote what we meant to write. This asserts
lftp *accepts* it. The oracle is the binary, which is the only thing that actually knows.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from lftpweb.core.lftp import HostCreds, build_rc_text

pytestmark = pytest.mark.skipif(
    shutil.which("lftp") is None,
    reason="lftp binary not on PATH -- install lftp to validate generated settings",
)


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


def _set_lines(rc: str) -> list[str]:
    """Only the `set` lines — `open`/credential lines would try to reach a host."""
    return [
        line.strip().rstrip(";")
        for line in rc.splitlines()
        if line.strip().startswith("set ") and "password" not in line
    ]


@pytest.mark.parametrize("pget_n", [1, 4])
@pytest.mark.parametrize("rate_limit_bps", [None, 1_000_000])
def test_every_generated_setting_is_accepted_by_lftp(pget_n, rate_limit_bps):
    rc = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=rate_limit_bps,
        connection_limit=8,
        parallel=4,
        pget_n=pget_n,
        save_status_interval_s=1,
        extra_settings="",
    )
    lines = _set_lines(rc)
    assert lines, "expected the rc to contain settings"

    for line in lines:
        result = subprocess.run(  # noqa: S603
            ["lftp", "-c", line],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (result.stdout + result.stderr).strip()
        assert not output, f"lftp rejected `{line}`: {output}"
        assert result.returncode == 0, f"lftp exited {result.returncode} for `{line}`"


def test_extra_lftp_settings_override_a_colliding_lftpweb_default(tmp_path):
    """Whether an "extra lftp settings" line that names a key lftpweb already sets actually
    *wins* is a behavioural claim about lftp's own `set` parser, not something this project
    controls — so it must be proven against the real binary, not assumed, before the Settings
    → Transfer UI is allowed to say anything about which side wins a collision.

    Verified interactively against lftp 4.9.2 first (`lftp -c "set K v1; set K v2; set -a"`
    prints only `v2`), then reproduced here through the exact rc `build_rc_text` generates,
    `source`d the same way `core/lftp.py.spawn` sources it — extra_settings is appended after
    every built-in tuning line and before the credential-bearing `open`, so a colliding key
    here is the same shape a real job would `source`.
    """
    rc = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="set pget:min-chunk-size 999999;",
    )
    # lftpweb's own default line is still present, ahead of the override — this proves the
    # test would catch a regression where extra_settings stopped being appended last, rather
    # than passing vacuously because the built-in line was never there to collide with.
    assert "set pget:min-chunk-size 1048576;" in rc
    assert rc.index("set pget:min-chunk-size 1048576;") < rc.index(
        "set pget:min-chunk-size 999999;"
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
    altered = [
        line for line in result.stdout.splitlines() if line.startswith("set pget:min-chunk-size ")
    ]
    assert altered == [
        "set pget:min-chunk-size 999999"
    ], f"expected the later extra_settings line to win (last-write-wins), got: {altered}"
