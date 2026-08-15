"""`GET /api/settings/transfer/effective-lftp` (2026-08-14,
prompts/2026-08-14-show-effective-lftp-settings.md) -- the read-only "what lftpweb already
sets" readout next to Settings -> Transfer's "Extra lftp settings" box.

Two things are load-bearing here, same shape as `tests/test_backup.py`'s
`test_backup_never_contains_the_encryption_secret`:

1. Neither the seedbox password nor the ssh key path/material ever appears in the response --
   proven absent by byte-search, not assumed absent, with a positive control proving the
   password really was reachable to *some* code path (a real `build_rc_text` call) so this
   test would actually catch a regression rather than passing vacuously.
2. The response tracks `TransferSettings` -- change a setting, see the new value reflected --
   which is what proves the endpoint is generated from the live settings rather than a
   hardcoded string that happens to match today's defaults.
"""

from __future__ import annotations

import asyncssh
from fastapi.testclient import TestClient

from lftpweb.core.lftp import HostCreds, build_rc_text
from lftpweb.main import app


def _generate_test_key_pem() -> str:
    key = asyncssh.generate_private_key("ssh-ed25519")
    return key.export_private_key().decode("ascii")


def test_effective_lftp_settings_never_contains_the_password(isolated_config):
    password = "hunter2-seedbox-password"  # noqa: S105
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "seedbox.example",
                "port": 22,
                "username": "seeduser",
                "auth_method": "password",
                "password": password,
                "known_hosts_policy": "insecure",
            },
        )
        assert resp.status_code == 200

        resp = client.get("/api/settings/transfer/effective-lftp")
        assert resp.status_code == 200
        raw = resp.content

        assert password.encode("ascii") not in raw

    # Positive control (test_backup.py's own idiom): prove the password really was reachable
    # to code that intentionally builds a real rc file, so the assertion above would actually
    # catch a regression that started leaking it, rather than passing because nothing in this
    # test ever put the password anywhere.
    creds = HostCreds(
        address="seedbox.example",
        port=22,
        username="seeduser",
        auth_method="password",
        key_path=None,
        password=password,
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
    assert password in rc


def test_effective_lftp_settings_never_contains_the_ssh_key_or_its_path(isolated_config):
    key_pem = _generate_test_key_pem()
    key_path = "/config/keys/id_ed25519_super_secret_path"
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "seedbox.example",
                "port": 22,
                "username": "seeduser",
                "auth_method": "key",
                "key_path": key_path,
                "ssh_key": key_pem,
                "known_hosts_policy": "insecure",
            },
        )
        assert resp.status_code == 200

        resp = client.get("/api/settings/transfer/effective-lftp")
        assert resp.status_code == 200
        raw = resp.content

        assert key_path.encode("ascii") not in raw
        # The PEM body is far too distinctive to appear by accident; checking the BEGIN marker
        # plus a chunk of the base64 body is enough to prove the whole key never round-trips.
        assert b"PRIVATE KEY" not in raw
        for line in key_pem.splitlines():
            if line and "-----" not in line:
                assert line.encode("ascii") not in raw

    # Positive control: the key path really does drive `sftp:connect-program` in a real rc.
    creds = HostCreds(
        address="seedbox.example",
        port=22,
        username="seeduser",
        auth_method="key",
        key_path=key_path,
        password=None,
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
    assert key_path in rc


def test_effective_lftp_settings_tracks_transfer_settings(isolated_config):
    """Change a setting through the real PUT, see the new number reflected -- proof this is
    generated from `TransferSettings`, not a hand-maintained string.
    """
    with TestClient(app) as client:
        resp = client.get("/api/settings/transfer")
        assert resp.status_code == 200
        current = resp.json()

        resp = client.get("/api/settings/transfer/effective-lftp")
        assert resp.status_code == 200
        before = resp.json()
        mirror_kind = next(k for k in before["kinds"] if k["kind"] == "mirror")
        assert f"--parallel={current['mirror_parallel_transfer_count']}" in mirror_kind["argv"]

        current["mirror_parallel_transfer_count"] = current["mirror_parallel_transfer_count"] + 3
        current["mirror_use_pget_n"] = current["mirror_use_pget_n"] + 5
        resp = client.put("/api/settings/transfer", json=current)
        assert resp.status_code == 200

        resp = client.get("/api/settings/transfer/effective-lftp")
        assert resp.status_code == 200
        after = resp.json()
        mirror_kind = next(k for k in after["kinds"] if k["kind"] == "mirror")
        assert f"--parallel={current['mirror_parallel_transfer_count']}" in mirror_kind["argv"]
        assert f"--use-pget-n={current['mirror_use_pget_n']}" in mirror_kind["argv"]

        pget_n_setting = next(
            s for s in mirror_kind["rc_settings"] if s["key"] == "mirror:use-pget-n"
        )
        assert pget_n_setting["value"] == str(current["mirror_use_pget_n"])
        assert pget_n_setting["configurable"] is True


def test_effective_lftp_settings_has_no_queue_context_but_documents_exclude_glob(
    isolated_config,
):
    """No queue is configured in this test, so `--exclude-glob` cannot be computed for real --
    the mirror kind's `argv_why` documents that instead of the endpoint pretending to compute
    it (see api/jobs.py._ARGV_WHY).
    """
    with TestClient(app) as client:
        resp = client.get("/api/settings/transfer/effective-lftp")
        assert resp.status_code == 200
        body = resp.json()
        mirror_kind = next(k for k in body["kinds"] if k["kind"] == "mirror")
        assert "--exclude-glob" not in mirror_kind["argv"]
        assert "--exclude-glob" in mirror_kind["argv_why"]
