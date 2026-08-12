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
