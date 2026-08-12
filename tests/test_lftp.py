"""core/lftp.py's pure functions — command/rc-file construction and error classification.
Real subprocess spawning against the fake seedbox is exercised in tests/test_queue.py and the
phase 3 report's manual verification (DESIGN.md §1.3: this module is deliberately the only
place lftp's actual output is ever read, and only for classification on a non-zero exit).
"""

from __future__ import annotations

import pytest

from lftpweb.core.lftp import (
    HostCreds,
    NoHostKeyPinError,
    build_rc_text,
    build_transfer_command,
    classify_output,
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
        ("ls: ssh: Could not resolve hostname nosuchhost.invalid: Name or service not known", "HOST_UNREACHABLE"),
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
        _creds(), None, rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert not text.startswith("\n")
    assert text.splitlines()[0].strip() != ""


def test_rc_text_password_auth_uses_open_dash_u_with_password():
    text = build_rc_text(
        _creds(auth_method="password", password="hunter2"), None,
        rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "open -u 'seeduser','hunter2' sftp://seedbox.example:2222;" in text


def test_rc_text_key_auth_uses_empty_password_field():
    # Found running this against the fake seedbox: a bare `open sftp://user@host` makes
    # lftp's own sftp backend try to prompt for a password itself instead of deferring to the
    # connect-program's ssh identity — even though that ssh already authenticates via the key.
    # `-u user,` with an empty password field is what avoids it.
    text = build_rc_text(
        _creds(auth_method="key", key_path="/config/keys/id_ed25519", password=None), None,
        rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "open -u 'seeduser','' sftp://seedbox.example:2222;" in text
    assert "-i /config/keys/id_ed25519" in text


def test_rc_text_password_auth_requires_a_password():
    with pytest.raises(ValueError):
        build_rc_text(
            _creds(auth_method="password", password=None), None,
            rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
            save_status_interval_s=1, extra_settings="",
        )


def test_rc_text_insecure_policy_disables_host_key_checking():
    text = build_rc_text(
        _creds(known_hosts_policy="insecure"), None,
        rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "StrictHostKeyChecking=no" in text
    assert "UserKnownHostsFile=/dev/null" in text


def test_rc_text_strict_policy_without_a_pin_refuses_to_build():
    with pytest.raises(NoHostKeyPinError):
        build_rc_text(
            _creds(known_hosts_policy="strict", pinned_host_key=None), None,
            rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
            save_status_interval_s=1, extra_settings="",
        )


def test_rc_text_strict_policy_with_a_pin_uses_the_known_hosts_file(tmp_path):
    kh_path = tmp_path / "job-1.known_hosts"
    kh_path.write_text("seedbox.example ssh-ed25519 AAAA...\n")
    text = build_rc_text(
        _creds(known_hosts_policy="strict"), kh_path,
        rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "StrictHostKeyChecking=yes" in text
    assert f"UserKnownHostsFile={kh_path}" in text


def test_rc_text_includes_rate_limit_and_connection_limit_when_given():
    text = build_rc_text(
        _creds(), None, rate_limit_bps=5_000_000, connection_limit=8, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "set net:limit-total-rate 5000000;" in text
    assert "set net:connection-limit 8;" in text


def test_rc_text_omits_rate_limit_line_when_unset():
    text = build_rc_text(
        _creds(), None, rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "net:limit-total-rate" not in text


def test_rc_text_lowers_pget_save_status_from_lftps_slow_10s_default():
    # lftp's own default (`pget:save-status 10s`) is too coarse for a ~1 Hz sampler — found
    # running a real transfer and seeing no sidecar at all at the 1s/2s/3s marks.
    text = build_rc_text(
        _creds(), None, rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert "set pget:save-status 1s;" in text


def test_rc_text_sets_temp_file_convention_matching_local_scan():
    text = build_rc_text(
        _creds(), None, rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="",
    )
    assert 'set xfer:use-temp-file yes;' in text
    assert 'set xfer:temp-file-name "*.lftp";' in text


def test_rc_text_appends_extra_settings_verbatim():
    text = build_rc_text(
        _creds(), None, rate_limit_bps=None, connection_limit=None, parallel=1, pget_n=1,
        save_status_interval_s=1, extra_settings="set net:socket-buffer 262144;",
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
        "mirror", "/remote/Some.Release", "/downloads", parallel=1, pget_n=1, exclude_globs=("*.nfo", "*SAMPLE*")
    )
    assert "--exclude-glob '*.nfo'" in cmd
    assert "--exclude-glob '*SAMPLE*'" in cmd


def test_quoting_escapes_embedded_single_quotes():
    cmd = build_transfer_command(
        "pget", "/remote/it's a file.mkv", "/downloads/it's a file.mkv", parallel=1, pget_n=1, exclude_globs=()
    )
    assert cmd == "pget -c -n 1 '/remote/it'\\''s a file.mkv' -o '/downloads/it'\\''s a file.mkv'"
