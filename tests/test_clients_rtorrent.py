"""Tests for `core/clients/rtorrent.py` (docs/download-client-framework-spec.md §14 stage 1/2,
prompts/2026-08-22-rtorrent-connector.md) -- over `tests/fake_rtorrent.py`'s real uvicorn socket.

**Every fixture response and every status-mapping constant this test drives is authored from
vendor documentation and is UNVERIFIED against a live rTorrent** -- see `fake_rtorrent.py`'s and
`rtorrent.py`'s own module docstrings, and spec §13.6 for the risk-ranked correction list. These
tests prove the connector matches *this module's own reading* of the vendor docs, not that the
reading is correct.
"""

from __future__ import annotations

import pytest

from lftpweb.core.clients.base import Operation, project_transfer
from lftpweb.core.clients.errors import (
    CapabilityUnavailable,
    ClientAuthenticationFailed,
    ClientError,
    ClientUnreachable,
)
from lftpweb.core.clients.models import BasePathKind, TransferPhase
from lftpweb.core.clients.rtorrent import RtorrentClient
from fake_rtorrent import FakeRtorrentTorrent

UPPER_HASH = "12682AF0C00A061448BCFA16975A5D5F01A84A61"


def _client(server, *, username: str | None = None, password: str | None = None) -> RtorrentClient:
    return RtorrentClient(
        config={
            "base_url": server.base_url,
            "username": username if username is not None else server.state.username,
            "password": password if password is not None else server.state.password,
        }
    )


def _torrent(**overrides) -> FakeRtorrentTorrent:
    defaults = dict(
        torrent_hash=UPPER_HASH,
        name="Sample.Release.S01E01",
        size_bytes=2_000_000,
        completed_bytes=1_000_000,
        left_bytes=1_000_000,
        down_rate=100_000,
        up_total=500_000,
        ratio=1500,  # per-mille -> 1.5
        state=1,
        complete=0,
        is_active=1,
        hashing=0,
        message="",
        base_path="/downloads/rtorrent/Sample.Release.S01E01",
        custom1="tv",
        timestamp_started=1_700_000_000,
        timestamp_finished=0,
        free_diskspace=5_000_000_000,
    )
    defaults.update(overrides)
    return FakeRtorrentTorrent(**defaults)


# ------------------------------------------------------------------------------------
# test_connection
# ------------------------------------------------------------------------------------


async def test_connection_success_reports_version(fake_rtorrent_server):
    client = _client(fake_rtorrent_server)
    info = await client.test_connection()
    assert info.version == fake_rtorrent_server.state.client_version


async def test_connection_bad_credentials_raise_authentication_failed(fake_rtorrent_server):
    client = _client(fake_rtorrent_server, password="wrong-password")
    with pytest.raises(ClientAuthenticationFailed) as exc_info:
        await client.test_connection()
    assert isinstance(exc_info.value, ClientError)
    assert not isinstance(exc_info.value, ClientUnreachable)
    assert not isinstance(exc_info.value, CapabilityUnavailable)


async def test_connection_forced_unauthorized_raises_authentication_failed(fake_rtorrent_server):
    fake_rtorrent_server.state.force_unauthorized = True
    client = _client(fake_rtorrent_server)
    with pytest.raises(ClientAuthenticationFailed):
        await client.test_connection()


async def test_connection_unreachable_host_raises_client_unreachable():
    client = RtorrentClient(
        config={"base_url": "http://127.0.0.1:1", "username": "x", "password": "y"}
    )
    with pytest.raises(ClientUnreachable):
        await client.test_connection()


async def test_connection_server_error_raises_client_error(fake_rtorrent_server):
    fake_rtorrent_server.state.fail_all = True
    client = _client(fake_rtorrent_server)
    with pytest.raises(ClientError) as exc_info:
        await client.test_connection()
    assert not isinstance(exc_info.value, ClientUnreachable)
    assert not isinstance(exc_info.value, ClientAuthenticationFailed)


async def test_unrecognised_method_raises_capability_unavailable(fake_rtorrent_server):
    client = _client(fake_rtorrent_server)
    # `d.custom5.set` -- the erasedata hook's flag call -- must not exist as a code path this
    # connector can invoke, but the fixture itself answers *any* unrecognised method with the
    # same "could not find command" fault a bare rTorrent gives, so this also proves `_call`'s
    # fault classification actually reads as CapabilityUnavailable for that shape of fault.
    with pytest.raises(CapabilityUnavailable):
        await client._call("d.custom5.set", UPPER_HASH, "1")


# ------------------------------------------------------------------------------------
# map_phase totality (spec §3) -- every recognised token, plus garbage input.
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("hashing", TransferPhase.VERIFYING),
        ("seeding", TransferPhase.SEEDING),
        ("completed", TransferPhase.COMPLETED),
        ("downloading", TransferPhase.DOWNLOADING),
        ("paused", TransferPhase.PAUSED),
        ("queued", TransferPhase.QUEUED),
    ],
)
def test_map_phase_recognised_tokens(token, expected):
    assert RtorrentClient.map_phase(token) is expected


@pytest.mark.parametrize("garbage", ["", "???", "DOWNLOADING", "12345", "🎉", "Seeding"])
def test_map_phase_is_total_for_unrecognised_input(garbage):
    assert RtorrentClient.map_phase(garbage) is TransferPhase.UNKNOWN


# ------------------------------------------------------------------------------------
# list_transfers -- flag combinations -> phase, per prompt's own table plus the
# PAUSED/QUEUED elaboration this connector adds on top of it.
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected_phase,expected_token",
    [
        ({"hashing": 1}, TransferPhase.VERIFYING, "hashing"),
        ({"complete": 1, "is_active": 1}, TransferPhase.SEEDING, "seeding"),
        ({"complete": 1, "is_active": 0}, TransferPhase.COMPLETED, "completed"),
        ({"complete": 0, "is_active": 1}, TransferPhase.DOWNLOADING, "downloading"),
        ({"complete": 0, "is_active": 0, "state": 0}, TransferPhase.PAUSED, "paused"),
        ({"complete": 0, "is_active": 0, "state": 1}, TransferPhase.QUEUED, "queued"),
    ],
)
async def test_list_transfers_flag_combinations_map_to_expected_phase(
    fake_rtorrent_server, overrides, expected_phase, expected_token
):
    fake_rtorrent_server.state.add(_torrent(**overrides))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.phase is expected_phase
    assert result.raw_status == expected_token
    # raw_status and phase are produced by the same classification, per this connector's own
    # documented decision -- never disagree with each other.
    assert RtorrentClient.map_phase(result.raw_status) is result.phase


async def test_hashing_overrides_complete_and_active_flags(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(hashing=1, complete=1, is_active=1))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.phase is TransferPhase.VERIFYING


# ------------------------------------------------------------------------------------
# Infohash normalization (spec §7.1) -- uppercase in, lowercase out for storage/comparison.
# ------------------------------------------------------------------------------------


async def test_client_id_normalized_to_lowercase_from_uppercase_wire_value(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent())
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.client_id == UPPER_HASH.lower()


async def test_per_item_calls_send_the_hash_back_uppercase(fake_rtorrent_server):
    """The trap this connector's own `_to_rtorrent_hash` exists for: a caller (the poller, the
    delete pipeline) only ever holds the lowercase-normalized id (spec §7.1). If this connector
    forgot to uppercase it again before calling back into rTorrent, the fixture's case-sensitive
    lookup would raise "could not find info-hash" here -- this test would fail with a raised
    `ClientError`/`CapabilityUnavailable` instead of passing.
    """
    fake_rtorrent_server.state.add(_torrent())
    client = _client(fake_rtorrent_server)
    lowercase_id = UPPER_HASH.lower()

    await client.pause(lowercase_id)
    await client.resume(lowercase_id)

    calls = fake_rtorrent_server.state.action_calls
    pause_call = next(c for c in calls if c["method"] == "d.pause")
    resume_call = next(c for c in calls if c["method"] == "d.resume")
    assert pause_call["params"] == [UPPER_HASH]
    assert resume_call["params"] == [UPPER_HASH]


# ------------------------------------------------------------------------------------
# Per-mille ratio conversion (spec §2.2, §5).
# ------------------------------------------------------------------------------------


async def test_ratio_is_divided_by_one_thousand(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(ratio=1500))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.ratio == 1.5


# ------------------------------------------------------------------------------------
# Derived seed-time (spec §4.3) -- carries its note, and is None while incomplete.
# ------------------------------------------------------------------------------------


async def test_seed_time_derived_field_carries_its_caveat_note():
    """`TORRENT_BASELINE` itself declares this key `NATIVE` with no note -- this connector
    overrides it to `DERIVED` with a note (see `RtorrentClient.capabilities`'s own comment): the
    baseline's declaration is a pre-existing inconsistency with spec §4.3's own canonical worked
    example for this exact field, not something this connector should propagate.
    """
    caps = RtorrentClient.capabilities
    from lftpweb.core.clients.base import Field, Support

    cap = caps.fields[Field.SEED_TIME_S]
    assert cap.support is Support.DERIVED
    assert "wall-clock" in (cap.note or "")


async def test_seed_time_populated_only_once_complete(fake_rtorrent_server):
    import time

    now = int(time.time())
    fake_rtorrent_server.state.add(_torrent(complete=1, is_active=0, timestamp_finished=now - 3600))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.seed_time_s is not None
    assert result.seed_time_s >= 3600 - 5  # allow a little test-execution slack


async def test_seed_time_none_while_incomplete(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(complete=0, is_active=1, timestamp_finished=0))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.seed_time_s is None


# ------------------------------------------------------------------------------------
# eta_s -- derived (this connector's own override of TORRENT_BASELINE's NATIVE claim).
# ------------------------------------------------------------------------------------


async def test_eta_declared_derived_not_native():
    from lftpweb.core.clients.base import Field, Support

    cap = RtorrentClient.capabilities.fields[Field.ETA_S]
    assert cap.support is Support.DERIVED


async def test_eta_computed_from_left_bytes_and_down_rate(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(left_bytes=1_000_000, down_rate=100_000))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.eta_s == 10


async def test_eta_none_when_rate_is_zero(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(left_bytes=1_000_000, down_rate=0))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.eta_s is None


# ------------------------------------------------------------------------------------
# remove -- unregister only; d.stop then d.erase, never a data-deleting call (spec §10.1).
# ------------------------------------------------------------------------------------


async def test_remove_calls_stop_then_erase_and_nothing_else(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent())
    client = _client(fake_rtorrent_server)

    outcome = await client.remove(UPPER_HASH.lower())

    assert outcome.succeeded is True
    methods = [c["method"] for c in fake_rtorrent_server.state.action_calls]
    assert methods == ["d.stop", "d.erase"]
    # The single hardest safety property this connector has: no code path here can ever call
    # the erasedata hook sequence, and this asserts it never did.
    assert "d.custom5.set" not in methods
    assert "d.delete_tied" not in methods
    assert UPPER_HASH not in fake_rtorrent_server.state.torrents


async def test_remove_of_unknown_hash_reports_failure_not_a_raise(fake_rtorrent_server):
    client = _client(fake_rtorrent_server)

    outcome = await client.remove("does-not-exist-anywhere-40-chars-000000")

    assert outcome.succeeded is False


async def test_remove_never_calls_erasedata_hook_sequence_across_many_calls(fake_rtorrent_server):
    """A broader regression net than the single-call assertion above: seed several torrents,
    remove them all, and assert the *entire* fixture-observed call log never contains either
    erasedata-hook method, across the whole session -- not just the one call under test.
    """
    for i in range(3):
        fake_rtorrent_server.state.add(_torrent(torrent_hash=f"{i:040d}".upper(), name=f"item-{i}"))
    client = _client(fake_rtorrent_server)

    for i in range(3):
        await client.remove(f"{i:040d}")

    methods = {c["method"] for c in fake_rtorrent_server.state.action_calls}
    assert methods == {"d.stop", "d.erase"}


# ------------------------------------------------------------------------------------
# list_base_paths -- directory.default, reported WORKING.
# ------------------------------------------------------------------------------------


async def test_list_base_paths_reports_directory_default_as_working(fake_rtorrent_server):
    fake_rtorrent_server.state.directory_default = "/downloads/rtorrent"
    client = _client(fake_rtorrent_server)

    paths = await client.list_base_paths()

    assert len(paths) == 1
    assert paths[0].path == "/downloads/rtorrent"
    assert paths[0].kind is BasePathKind.WORKING


# ------------------------------------------------------------------------------------
# list_trackers -- hostname only, never the full announce URL (spec §7.3).
# ------------------------------------------------------------------------------------


async def test_list_trackers_returns_hostname_only(fake_rtorrent_server):
    torrent = _torrent()
    torrent.trackers = ["https://tracker.example.test:443/announce?passkey=SECRETVALUE"]
    fake_rtorrent_server.state.add(torrent)
    client = _client(fake_rtorrent_server)

    trackers = await client.list_trackers(UPPER_HASH.lower())

    assert len(trackers) == 1
    assert trackers[0].host == "tracker.example.test:443"
    assert "SECRETVALUE" not in trackers[0].host
    assert "passkey" not in trackers[0].host


# ------------------------------------------------------------------------------------
# list_files.
# ------------------------------------------------------------------------------------


async def test_list_files_returns_paths(fake_rtorrent_server):
    torrent = _torrent()
    torrent.files = ["Sample.Release.S01E01/a.mkv", "Sample.Release.S01E01/b.nfo"]
    fake_rtorrent_server.state.add(torrent)
    client = _client(fake_rtorrent_server)

    files = await client.list_files(UPPER_HASH.lower())

    assert files == torrent.files


# ------------------------------------------------------------------------------------
# free_space -- best-effort, matched via a known transfer's content_path.
# ------------------------------------------------------------------------------------


async def test_free_space_reads_via_a_matching_transfer(fake_rtorrent_server):
    fake_rtorrent_server.state.add(
        _torrent(base_path="/downloads/rtorrent/Sample.Release.S01E01", free_diskspace=123456)
    )
    client = _client(fake_rtorrent_server)

    info = await client.free_space("/downloads/rtorrent/Sample.Release.S01E01")

    assert info.free_bytes == 123456
    assert info.total_bytes is None


async def test_free_space_raises_client_error_when_nothing_matches(fake_rtorrent_server):
    client = _client(fake_rtorrent_server)
    with pytest.raises(ClientError):
        await client.free_space("/no/such/path")


# ------------------------------------------------------------------------------------
# content_path -- d.base_path, the seeding location (spec §1.1), never the hardlinked copy.
# ------------------------------------------------------------------------------------


async def test_content_path_comes_from_base_path(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(base_path="/downloads/rtorrent/Sample.Release.S01E01"))
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)

    assert result.content_path == "/downloads/rtorrent/Sample.Release.S01E01"


# ------------------------------------------------------------------------------------
# list_transfers(active_only=True) excludes only COMPLETED.
# ------------------------------------------------------------------------------------


async def test_active_only_excludes_completed_but_keeps_seeding(fake_rtorrent_server):
    fake_rtorrent_server.state.add(
        _torrent(torrent_hash="A" * 40, complete=1, is_active=0)  # completed
    )
    fake_rtorrent_server.state.add(
        _torrent(torrent_hash="B" * 40, complete=1, is_active=1)  # seeding
    )
    fake_rtorrent_server.state.add(
        _torrent(torrent_hash="C" * 40, complete=0, is_active=1)  # downloading
    )
    client = _client(fake_rtorrent_server)

    active = await client.list_transfers(active_only=True)
    phases = {t.phase for t in active}

    assert TransferPhase.COMPLETED not in phases
    assert TransferPhase.SEEDING in phases
    assert TransferPhase.DOWNLOADING in phases


# ------------------------------------------------------------------------------------
# list_history -- derived: SEEDING and COMPLETED only.
# ------------------------------------------------------------------------------------


async def test_list_history_includes_seeding_and_completed_only(fake_rtorrent_server):
    fake_rtorrent_server.state.add(
        _torrent(torrent_hash="A" * 40, complete=1, is_active=0)  # completed
    )
    fake_rtorrent_server.state.add(
        _torrent(torrent_hash="B" * 40, complete=1, is_active=1)  # seeding
    )
    fake_rtorrent_server.state.add(
        _torrent(torrent_hash="C" * 40, complete=0, is_active=1)  # downloading
    )
    client = _client(fake_rtorrent_server)

    history = await client.list_history()
    phases = {t.phase for t in history}

    assert phases == {TransferPhase.COMPLETED, TransferPhase.SEEDING}


# ------------------------------------------------------------------------------------
# get_transfer -- derived, filters the merged list.
# ------------------------------------------------------------------------------------


async def test_get_transfer_finds_by_normalized_id(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent())
    client = _client(fake_rtorrent_server)

    found = await client.get_transfer(UPPER_HASH.lower())
    missing = await client.get_transfer("0" * 40)

    assert found is not None
    assert found.client_id == UPPER_HASH.lower()
    assert missing is None


# ------------------------------------------------------------------------------------
# set_label -- writes d.custom1.set.
# ------------------------------------------------------------------------------------


async def test_set_label_writes_custom1(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent())
    client = _client(fake_rtorrent_server)

    await client.set_label(UPPER_HASH.lower(), "movies")

    assert fake_rtorrent_server.state.torrents[UPPER_HASH].custom1 == "movies"


# ------------------------------------------------------------------------------------
# list_categories (spec §2.1, §8.3, joined 2026-08-23) -- doc-derived, UNVERIFIED. Deduplicated
# `d.custom1` off the same listing multicall; declared `Support.DERIVED` on `capabilities`
# because this can only ever report labels currently in use, never rTorrent's full universe of
# labels ever typed.
# ------------------------------------------------------------------------------------


async def test_list_categories_declared_derived_not_native():
    from lftpweb.core.clients.base import Operation, Support

    cap = RtorrentClient.capabilities.operations[Operation.LIST_CATEGORIES]
    assert cap.support is Support.DERIVED
    assert "labels currently assigned" in (cap.note or "")


async def test_list_categories_returns_distinct_labels_in_use(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(torrent_hash="A" * 40, custom1="tv"))
    fake_rtorrent_server.state.add(_torrent(torrent_hash="B" * 40, custom1="movies"))
    fake_rtorrent_server.state.add(_torrent(torrent_hash="C" * 40, custom1="tv"))
    client = _client(fake_rtorrent_server)

    categories = await client.list_categories()

    assert categories == ["movies", "tv"]


async def test_list_categories_excludes_torrents_with_no_label(fake_rtorrent_server):
    """Decision (a) of prompts/2026-08-23-category-binding-redesign.md: an rTorrent torrent with
    no `d.custom1` label contributes no category, and this connector never invents an
    "(no category)" pseudo-entry for it -- `list_categories` simply omits it, the same silent-
    omission rule spec §8.3 already applies to an item that can't be attributed to a queue.
    """
    fake_rtorrent_server.state.add(_torrent(torrent_hash="A" * 40, custom1=""))
    fake_rtorrent_server.state.add(_torrent(torrent_hash="B" * 40, custom1="tv"))
    client = _client(fake_rtorrent_server)

    categories = await client.list_categories()

    assert categories == ["tv"]


async def test_list_categories_empty_when_nothing_is_labelled(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent(torrent_hash="A" * 40, custom1=""))
    client = _client(fake_rtorrent_server)

    assert await client.list_categories() == []


# ------------------------------------------------------------------------------------
# The capability declaration matches what this connector can actually populate.
# ------------------------------------------------------------------------------------


async def test_capability_declaration_matches_real_populated_fields(fake_rtorrent_server):
    fake_rtorrent_server.state.add(_torrent())
    client = _client(fake_rtorrent_server)

    (result,) = await client.list_transfers(active_only=False)
    reprojected = project_transfer(result, client.capabilities)

    assert reprojected == result
    assert client.capabilities.supports(Operation.LIST_TRACKERS) is True
    assert client.capabilities.supports(Operation.RECHECK) is True
