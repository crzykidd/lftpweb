"""core/lftp.py's pure functions — command/rc-file construction and error classification.
Real subprocess spawning against the fake seedbox is exercised in tests/test_queue.py and the
phase 3 report's manual verification (DESIGN.md §1.3: this module is deliberately the only
place lftp's actual output is ever read, and only for classification on a non-zero exit).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from lftpweb.core.lftp import (
    HostCreds,
    JobSpec,
    NoHostKeyPinError,
    build_rc_text,
    build_transfer_command,
    classify_output,
    spawn,
    terminate,
)


def _creds(**overrides) -> HostCreds:
    base = dict(
        address="seedbox.example",
        port=2222,
        username="seeduser",
        auth_method="password",
        key_path=None,
        password="testpass123",
        known_hosts_policy="insecure",
        pinned_host_key=None,
    )
    base.update(overrides)
    return HostCreds(**base)


# --- Error classification — patterns matched against real lftp 4.9.2 output (see the phase 3
# report for the exact commands run against the fake seedbox) --------------------------------


@pytest.mark.parametrize(
    "output,expected",
    [
        ("ls: Login failed: Login incorrect", "AUTH_FAILED"),
        ("mirror: Access failed: Permission denied (/data/pickup/noperm)", "PERMISSION_DENIED"),
        ("pget: /data/pickup/does-not-exist.file: Access failed: No such file", "REMOTE_GONE"),
        (
            "pget: /data/x.mkv: /downloads/x.mkv: No space left on device",
            "DISK_FULL",
        ),
        (
            "ls: Fatal error: max-retries exceeded (connect to host 127.0.0.1 port 2299: Connection refused)",
            "HOST_UNREACHABLE",
        ),
        (
            "ls: ssh: Could not resolve hostname nosuchhost.invalid: Name or service not known",
            "HOST_UNREACHABLE",
        ),
        ("something about a TLS handshake failure", "TLS_ERROR"),
        ("a completely unrecognized error string", "UNKNOWN"),
    ],
)
def test_classify_output_matches_real_lftp_error_text(output, expected):
    assert classify_output(output) == expected


def test_classify_output_permission_denied_takes_priority_over_generic_host_text():
    # "Permission denied" also appears in ssh's own publickey-rejection message; the
    # AUTH_FAILED pattern is checked first specifically for that phrase.
    assert classify_output("Permission denied (publickey).") == "AUTH_FAILED"


# --- rc file construction ----------------------------------------------------------------


def test_rc_text_never_starts_with_a_blank_line():
    # A real lftp 4.9.2 parser quirk found building this module: a script whose first line is
    # blank corrupts quote-stripping on the next `set key "value"` line. See the module
    # docstring for the reproduction.
    text = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert not text.startswith("\n")
    assert text.splitlines()[0].strip() != ""


def test_rc_text_password_auth_uses_open_dash_u_with_password():
    text = build_rc_text(
        _creds(auth_method="password", password="hunter2"),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "open -u 'seeduser','hunter2' sftp://seedbox.example:2222;" in text


def test_rc_text_key_auth_uses_empty_password_field():
    # Found running this against the fake seedbox: a bare `open sftp://user@host` makes
    # lftp's own sftp backend try to prompt for a password itself instead of deferring to the
    # connect-program's ssh identity — even though that ssh already authenticates via the key.
    # `-u user,` with an empty password field is what avoids it.
    text = build_rc_text(
        _creds(auth_method="key", key_path="/config/keys/id_ed25519", password=None),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "open -u 'seeduser','' sftp://seedbox.example:2222;" in text
    assert "-i /config/keys/id_ed25519" in text


def test_rc_text_password_auth_requires_a_password():
    with pytest.raises(ValueError):
        build_rc_text(
            _creds(auth_method="password", password=None),
            None,
            rate_limit_bps=None,
            connection_limit=None,
            parallel=1,
            pget_n=1,
            save_status_interval_s=1,
            extra_settings="",
        )


def test_rc_text_insecure_policy_disables_host_key_checking():
    text = build_rc_text(
        _creds(known_hosts_policy="insecure"),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "StrictHostKeyChecking=no" in text
    assert "UserKnownHostsFile=/dev/null" in text


def test_rc_text_strict_policy_without_a_pin_refuses_to_build():
    with pytest.raises(NoHostKeyPinError):
        build_rc_text(
            _creds(known_hosts_policy="strict", pinned_host_key=None),
            None,
            rate_limit_bps=None,
            connection_limit=None,
            parallel=1,
            pget_n=1,
            save_status_interval_s=1,
            extra_settings="",
        )


def test_rc_text_strict_policy_with_a_pin_uses_the_known_hosts_file(tmp_path):
    kh_path = tmp_path / "job-1.known_hosts"
    kh_path.write_text("seedbox.example ssh-ed25519 AAAA...\n")
    text = build_rc_text(
        _creds(known_hosts_policy="strict"),
        kh_path,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "StrictHostKeyChecking=yes" in text
    assert f"UserKnownHostsFile={kh_path}" in text


def test_rc_text_includes_rate_limit_and_connection_limit_when_given():
    text = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=5_000_000,
        connection_limit=8,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "set net:limit-total-rate 5000000;" in text
    assert "set net:connection-limit 8;" in text


def test_rc_text_omits_rate_limit_line_when_unset():
    text = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "net:limit-total-rate" not in text


def test_rc_text_lowers_pget_save_status_from_lftps_slow_10s_default():
    # lftp's own default (`pget:save-status 10s`) is too coarse for a ~1 Hz sampler — found
    # running a real transfer and seeing no sidecar at all at the 1s/2s/3s marks.
    text = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "set pget:save-status 1s;" in text


def test_rc_text_sets_temp_file_convention_matching_local_scan():
    text = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "set xfer:use-temp-file yes;" in text
    assert 'set xfer:temp-file-name "*.lftp";' in text


def test_rc_text_appends_extra_settings_verbatim():
    text = build_rc_text(
        _creds(),
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="set net:socket-buffer 262144;",
    )
    assert "set net:socket-buffer 262144;" in text


# --- transfer command construction --------------------------------------------------------


def test_pget_command_uses_exact_local_file_path():
    cmd = build_transfer_command(
        "pget", "/remote/Movie.mkv", "/downloads/Movie.mkv", parallel=1, pget_n=4, exclude_globs=()
    )
    assert cmd == "pget -c -n 4 '/remote/Movie.mkv' -o '/downloads/Movie.mkv'"


def test_mirror_command_targets_the_parent_directory_not_the_item_directory():
    # The load-bearing, non-obvious lftp behavior found building this: mirror appends the
    # remote path's own basename onto the target, so the *parent* must be passed, not
    # `<parent>/<item>` — the latter produces a doubly-nested `<item>/<item>/...` tree.
    cmd = build_transfer_command(
        "mirror", "/remote/Some.Release", "/downloads", parallel=4, pget_n=4, exclude_globs=()
    )
    assert cmd == "mirror -c --parallel=4 --use-pget-n=4 '/remote/Some.Release' '/downloads/'"


def test_mirror_command_includes_exclude_globs():
    cmd = build_transfer_command(
        "mirror",
        "/remote/Some.Release",
        "/downloads",
        parallel=1,
        pget_n=1,
        exclude_globs=("*.nfo", "*SAMPLE*"),
    )
    assert "--exclude-glob '*.nfo'" in cmd
    assert "--exclude-glob '*SAMPLE*'" in cmd


def test_quoting_escapes_embedded_single_quotes():
    cmd = build_transfer_command(
        "pget",
        "/remote/it's a file.mkv",
        "/downloads/it's a file.mkv",
        parallel=1,
        pget_n=1,
        exclude_globs=(),
    )
    assert cmd == "pget -c -n 1 '/remote/it'\\''s a file.mkv' -o '/downloads/it'\\''s a file.mkv'"


def test_rc_always_bounds_retries_and_timeouts():
    """A connection that cannot succeed must fail fast, not retry forever.

    lftp's defaults retry indefinitely, so a bad host key or refused auth produced a process
    that never exited: the supervisor never saw a non-zero exit, `classify_output` never ran,
    and the job sat at DOWNLOADING/0 bytes with nothing in the log. Observed against a real
    seedbox. These settings are what turn that hang into a reportable failure.
    """
    creds = HostCreds(
        address="h",
        port=22,
        username="u",
        auth_method="password",
        key_path=None,
        password="p",
        known_hosts_policy="insecure",
        pinned_host_key=None,
    )
    rc = build_rc_text(
        creds,
        None,
        rate_limit_bps=None,
        connection_limit=None,
        parallel=1,
        pget_n=1,
        save_status_interval_s=1,
        extra_settings="",
    )
    assert "set net:max-retries 3;" in rc
    assert "set net:timeout 30s;" in rc
    # Bare number, not a time interval — lftp rejects "5s" here even though `net:timeout`
    # requires the suffix. tests/test_lftp_settings_accepted.py checks this against the
    # real binary; this line just pins the string.
    assert "set net:reconnect-interval-base 5;" in rc
    # One connection for anything under 1 MiB: `pget -n 4` on a 16-byte file otherwise opens
    # four SSH connections to move 16 bytes, and multiplies any handshake failure by four.
    assert "set pget:min-chunk-size 1048576;" in rc


# --- migration 014: per-job materialisation of a pasted key (DESIGN.md §8) ------------------
#
# Real authentication against the fake seedbox with a pasted key (both the asyncssh and lftp
# paths) is proven end to end in tests/test_ssh_key_e2e.py; these tests use an unreachable
# host (127.0.0.1:1, matching the "nothing listens here" convention in the classify_output
# tests above) so lftp's ssh child fails fast, and only check what `spawn()` puts on disk and
# what `cleanup()` removes -- never an actual transfer outcome.

_FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEYMATERIALFORTESTSONLY\n-----END OPENSSH PRIVATE KEY-----\n"


def _key_spec(tmp_path, **creds_overrides) -> JobSpec:
    base = dict(
        address="127.0.0.1",
        port=1,  # nothing listens here -- the ssh child fails immediately
        auth_method="key",
        key_path=None,
        password=None,
    )
    base.update(creds_overrides)
    creds = _creds(**base)
    return JobSpec(
        job_id=4242,
        kind="pget",
        creds=creds,
        remote_path="/remote/x",
        local_path=str(tmp_path / "x"),
        run_dir=str(tmp_path / "run"),
    )


async def test_spawn_materializes_a_pasted_key_to_a_per_job_tmpfs_file_mode_0600(tmp_path):
    job = await spawn(_key_spec(tmp_path, ssh_key=_FAKE_PEM))
    try:
        assert job.ssh_key_path is not None
        assert job.ssh_key_path.parent == Path(tmp_path / "run")
        assert job.ssh_key_path.read_text() == _FAKE_PEM
        assert stat.S_IMODE(job.ssh_key_path.stat().st_mode) == 0o600

        # The key text lives only in its own file -- never folded into the rc file, which
        # only references its path via `-i`.
        rc_text = job.rc_path.read_text()
        assert _FAKE_PEM not in rc_text
        assert f"-i {job.ssh_key_path}" in rc_text
    finally:
        await terminate(job)
        job.cleanup()

    assert not job.ssh_key_path.exists()
    assert not job.rc_path.exists()


async def test_spawn_without_a_pasted_key_never_creates_a_key_file(tmp_path):
    # `key_path` alone (the pre-existing, user-mounted case) must be untouched by this --
    # spawn() references it as-is and creates nothing of its own.
    job = await spawn(_key_spec(tmp_path, key_path="/mounted/operator-key"))
    try:
        assert job.ssh_key_path is None
        rc_text = job.rc_path.read_text()
        assert "-i /mounted/operator-key" in rc_text
    finally:
        await terminate(job)
        job.cleanup()


async def test_spawn_pasted_key_wins_over_key_path_and_never_touches_it(tmp_path):
    job = await spawn(_key_spec(tmp_path, key_path="/mounted/operator-key", ssh_key=_FAKE_PEM))
    try:
        assert job.ssh_key_path is not None
        rc_text = job.rc_path.read_text()
        # The materialized path is used, not the mounted one -- and the mounted path is
        # never created, chmod'd, or otherwise touched (it isn't ours to manage).
        assert f"-i {job.ssh_key_path}" in rc_text
        assert "/mounted/operator-key" not in rc_text
        assert not Path("/mounted/operator-key").exists()
    finally:
        await terminate(job)
        job.cleanup()


async def test_spawn_recreates_the_key_file_on_a_fresh_run_dir_no_startup_step_needed(tmp_path):
    """Simulates two spawns of the *same* job across a container restart: `/run` is emptied
    (a fresh `run_dir` here), and nothing but the encrypted DB row (represented here by
    `creds.ssh_key`, exactly what `core/engine.py.load_host_config` would hand back after
    decrypting) is needed to materialise the file again. There is no separate
    "re-materialise on startup" step to forget, because every spawn decrypts fresh --
    see `spawn()`'s own docstring.
    """
    job1 = await spawn(_key_spec(tmp_path, ssh_key=_FAKE_PEM))
    try:
        assert job1.ssh_key_path.exists()
    finally:
        await terminate(job1)
        job1.cleanup()
    assert not job1.ssh_key_path.exists()  # gone, as if `/run` had been wiped by a restart

    # "Restart": run_dir starts fresh again, same creds (as if freshly decrypted from the DB).
    job2 = await spawn(_key_spec(tmp_path, ssh_key=_FAKE_PEM))
    try:
        assert job2.ssh_key_path.exists()
        assert job2.ssh_key_path.read_text() == _FAKE_PEM
    finally:
        await terminate(job2)
        job2.cleanup()
