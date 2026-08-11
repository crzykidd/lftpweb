from __future__ import annotations

from lftpweb.core.remote import KnownHostsStore


def test_pin_and_get_round_trip(tmp_path):
    store = KnownHostsStore(tmp_path / "known_hosts.json")
    assert store.get("example.invalid", 22) is None

    store.pin("example.invalid", 22, "ssh-ed25519 AAAA...")
    assert store.get("example.invalid", 22) == "ssh-ed25519 AAAA..."


def test_pin_is_scoped_by_host_and_port(tmp_path):
    store = KnownHostsStore(tmp_path / "known_hosts.json")
    store.pin("example.invalid", 22, "key-a")
    store.pin("example.invalid", 2222, "key-b")
    assert store.get("example.invalid", 22) == "key-a"
    assert store.get("example.invalid", 2222) == "key-b"


def test_store_file_is_mode_0600(tmp_path):
    import stat

    store = KnownHostsStore(tmp_path / "known_hosts.json")
    store.pin("h", 22, "k")
    mode = (tmp_path / "known_hosts.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_corrupt_store_file_reads_as_empty_rather_than_raising(tmp_path):
    path = tmp_path / "known_hosts.json"
    path.write_text("not valid json{{{")
    store = KnownHostsStore(path)
    assert store.get("h", 22) is None
