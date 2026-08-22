"""Tests for `core/clients/sabnzbd.py` (docs/download-client-framework-spec.md §14 stage 1) --
over `tests/fake_sabnzbd.py`'s real uvicorn socket, `fake_arr.py`'s established pattern.

**Every fixture response and every status-mapping constant this test drives is authored from
vendor documentation and is UNVERIFIED against a live SABnzbd** -- see `fake_sabnzbd.py`'s and
`sabnzbd.py`'s own module docstrings. These tests prove the connector matches *a reading of the
documentation*, not that the reading is correct.
"""

from __future__ import annotations

import pytest

from lftpweb.core.clients.base import Operation, project_transfer
from lftpweb.core.clients.errors import CapabilityUnavailable, ClientError, ClientUnreachable
from lftpweb.core.clients.models import TransferPhase
from lftpweb.core.clients.sabnzbd import SabnzbdClient


def _client(server, *, api_key: str | None = None) -> SabnzbdClient:
    return SabnzbdClient(
        config={"base_url": server.base_url, "api_key": api_key or server.state.api_key}
    )


def _queue_slot(**overrides):
    slot = {
        "nzo_id": "SABnzbd_nzo_abc123",
        "filename": "Some.Release.S01E01",
        "status": "Downloading",
        "cat": "tv",
        "mb": "1500.00",
        "mbleft": "500.00",
        "timeleft": "0:10:00",
    }
    slot.update(overrides)
    return slot


def _history_slot(**overrides):
    slot = {
        "nzo_id": "SABnzbd_nzo_def456",
        "name": "Some.Other.Release.S01E02",
        "status": "Completed",
        "category": "tv",
        "storage": "/downloads/complete/tv/Some.Other.Release.S01E02",
        "bytes": 1_600_000_000,
        "fail_message": "",
        "completed": 1_700_000_000,
    }
    slot.update(overrides)
    return slot


# ------------------------------------------------------------------------------------
# test_connection
# ------------------------------------------------------------------------------------


async def test_connection_success_reports_version(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server)
    info = await client.test_connection()
    assert info.version == fake_sabnzbd_server.state.version


async def test_connection_bad_api_key_raises_client_error_not_unreachable(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server, api_key="wrong-key")
    with pytest.raises(ClientError) as exc_info:
        await client.test_connection()
    assert not isinstance(exc_info.value, ClientUnreachable)
    assert not isinstance(exc_info.value, CapabilityUnavailable)


async def test_connection_unreachable_host_raises_client_unreachable():
    # A closed port on localhost -- connection refused, no fake server involved at all.
    client = SabnzbdClient(config={"base_url": "http://127.0.0.1:1", "api_key": "x"})
    with pytest.raises(ClientUnreachable):
        await client.test_connection()


async def test_connection_server_error_raises_client_error(fake_sabnzbd_server):
    fake_sabnzbd_server.state.fail_all = True
    client = _client(fake_sabnzbd_server)
    with pytest.raises(ClientError) as exc_info:
        await client.test_connection()
    assert not isinstance(exc_info.value, ClientUnreachable)


# ------------------------------------------------------------------------------------
# list_transfers / list_history -- the two-source split (spec §2.1).
# ------------------------------------------------------------------------------------


async def test_list_transfers_active_only_reads_only_the_queue(fake_sabnzbd_server):
    fake_sabnzbd_server.state.queue_slots = [_queue_slot()]
    fake_sabnzbd_server.state.history_slots = [_history_slot()]
    client = _client(fake_sabnzbd_server)

    result = await client.list_transfers(active_only=True)

    assert [t.client_id for t in result] == ["SABnzbd_nzo_abc123"]
    assert result[0].phase is TransferPhase.DOWNLOADING


async def test_list_transfers_full_form_merges_queue_and_history(fake_sabnzbd_server):
    fake_sabnzbd_server.state.queue_slots = [_queue_slot()]
    fake_sabnzbd_server.state.history_slots = [_history_slot()]
    client = _client(fake_sabnzbd_server)

    result = await client.list_transfers(active_only=False)

    ids = {t.client_id for t in result}
    assert ids == {"SABnzbd_nzo_abc123", "SABnzbd_nzo_def456"}


async def test_list_history_content_path_comes_from_storage(fake_sabnzbd_server):
    fake_sabnzbd_server.state.history_slots = [_history_slot()]
    client = _client(fake_sabnzbd_server)

    (result,) = await client.list_history()

    assert result.content_path == "/downloads/complete/tv/Some.Other.Release.S01E02"
    assert result.phase is TransferPhase.COMPLETED


async def test_list_history_failed_item_carries_fail_message_as_error(fake_sabnzbd_server):
    fake_sabnzbd_server.state.history_slots = [
        _history_slot(
            nzo_id="SABnzbd_nzo_failed1",
            status="Failed",
            fail_message="Unpacking failed, use unrar? mode",
        )
    ]
    client = _client(fake_sabnzbd_server)

    (result,) = await client.list_history()

    assert result.phase is TransferPhase.FAILED
    assert result.error_message == "Unpacking failed, use unrar? mode"


async def test_history_item_with_no_failure_has_no_error_message(fake_sabnzbd_server):
    fake_sabnzbd_server.state.history_slots = [_history_slot(fail_message="")]
    client = _client(fake_sabnzbd_server)

    (result,) = await client.list_history()

    assert result.error_message is None


async def test_get_transfer_finds_an_item_in_either_source(fake_sabnzbd_server):
    fake_sabnzbd_server.state.queue_slots = [_queue_slot()]
    fake_sabnzbd_server.state.history_slots = [_history_slot()]
    client = _client(fake_sabnzbd_server)

    from_queue = await client.get_transfer("SABnzbd_nzo_abc123")
    from_history = await client.get_transfer("SABnzbd_nzo_def456")
    missing = await client.get_transfer("does-not-exist")

    assert from_queue is not None and from_queue.client_id == "SABnzbd_nzo_abc123"
    assert from_history is not None and from_history.content_path is not None
    assert missing is None


# ------------------------------------------------------------------------------------
# map_phase totality -- over the real transport, not only as a unit (spec §3, prompt item 4).
# ------------------------------------------------------------------------------------


async def test_unrecognised_queue_status_maps_to_unknown_over_real_transport(fake_sabnzbd_server):
    fake_sabnzbd_server.state.queue_slots = [
        _queue_slot(status="SomeBrandNewStatusSabHasNeverDocumented")
    ]
    client = _client(fake_sabnzbd_server)

    (result,) = await client.list_transfers(active_only=True)

    assert result.phase is TransferPhase.UNKNOWN
    assert result.raw_status == "SomeBrandNewStatusSabHasNeverDocumented"


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ("Queued", TransferPhase.QUEUED),
        ("Downloading", TransferPhase.DOWNLOADING),
        ("Paused", TransferPhase.PAUSED),
        ("Repairing", TransferPhase.VERIFYING),
        ("Extracting", TransferPhase.EXTRACTING),
        ("Moving", TransferPhase.EXTRACTING),
        ("Verifying", TransferPhase.VERIFYING),
        ("Fetching", TransferPhase.VERIFYING),
        ("Grabbing", TransferPhase.QUEUED),
        ("", TransferPhase.UNKNOWN),
    ],
)
def test_map_phase_documented_queue_vocabulary(raw_status, expected):
    assert SabnzbdClient.map_phase(raw_status) is expected


@pytest.mark.parametrize(
    "raw_status,expected",
    [("Completed", TransferPhase.COMPLETED), ("Failed", TransferPhase.FAILED)],
)
def test_map_phase_documented_history_vocabulary(raw_status, expected):
    assert SabnzbdClient.map_phase(raw_status) is expected


# ------------------------------------------------------------------------------------
# The v0.2.4-shaped blank-queue blip (spec §1, §4.2) -- no verdict, not "everything failed."
# ------------------------------------------------------------------------------------


async def test_blank_queue_blip_returns_empty_list_not_a_raise_or_a_verdict(fake_sabnzbd_server):
    fake_sabnzbd_server.state.queue_slots = [_queue_slot()]
    fake_sabnzbd_server.state.history_slots = [_history_slot()]
    fake_sabnzbd_server.state.queue_empty_for_requests = 1
    client = _client(fake_sabnzbd_server)

    during_blip = await client.list_transfers(active_only=True)
    assert during_blip == []  # no exception, and no synthesized "everything failed" record

    # The blip only ever touches `mode=queue` -- history (already-finished work) is completely
    # unaffected by it, proving the two sources are independent and a queue hiccup cannot
    # retroactively alter something the client already reported as finished.
    unaffected_history = await client.list_history()
    assert len(unaffected_history) == 1

    # And the very next queue call (the counter having reached zero) serves normally again --
    # the blip was transient, and this connector holds no local memory of the outage at all,
    # exactly the statelessness spec §1 relies on ("a connector is handed no database handle").
    recovered = await client.list_transfers(active_only=True)
    assert [t.client_id for t in recovered] == ["SABnzbd_nzo_abc123"]


# ------------------------------------------------------------------------------------
# free_space -- rides the queue call (spec's own survey note).
# ------------------------------------------------------------------------------------


async def test_free_space_reads_diskspace2_off_the_queue_response(fake_sabnzbd_server):
    fake_sabnzbd_server.state.diskspace2 = "42.50"
    fake_sabnzbd_server.state.diskspacetotal2 = "500.00"
    client = _client(fake_sabnzbd_server)

    info = await client.free_space("/downloads/complete")

    assert info.free_bytes == round(42.50 * 1024**3)
    assert info.total_bytes == round(500.00 * 1024**3)


async def test_free_space_ignores_the_path_argument(fake_sabnzbd_server):
    # SAB's diskspace fields carry no path association at all (doc-derived, UNVERIFIED) -- the
    # same call answers regardless of what `path` is, which this asserts directly.
    client = _client(fake_sabnzbd_server)
    a = await client.free_space("/some/path")
    b = await client.free_space("/a/totally/different/path")
    assert a == b


# ------------------------------------------------------------------------------------
# remove -- queue-then-history fallback, and the del_files=0 safety property (spec §10.1).
# ------------------------------------------------------------------------------------


async def test_remove_succeeds_from_the_queue_and_never_deletes_data(fake_sabnzbd_server):
    fake_sabnzbd_server.state.removable_from_queue = {"nzo-in-queue"}
    client = _client(fake_sabnzbd_server)

    outcome = await client.remove("nzo-in-queue")

    assert outcome.succeeded is True
    queue_delete_calls = [
        c
        for c in fake_sabnzbd_server.state.action_calls
        if c["mode"] == "queue" and c["name"] == "delete"
    ]
    assert queue_delete_calls[0]["del_files"] == "0"


async def test_remove_falls_back_to_history_when_not_in_the_queue(fake_sabnzbd_server):
    fake_sabnzbd_server.state.removable_from_history = {"nzo-in-history"}
    client = _client(fake_sabnzbd_server)

    outcome = await client.remove("nzo-in-history")

    assert outcome.succeeded is True
    history_delete_calls = [
        c
        for c in fake_sabnzbd_server.state.action_calls
        if c["mode"] == "history" and c["name"] == "delete"
    ]
    assert history_delete_calls[0]["del_files"] == "0"


async def test_remove_reports_failure_when_found_nowhere(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server)
    outcome = await client.remove("does-not-exist-anywhere")
    assert outcome.succeeded is False


# ------------------------------------------------------------------------------------
# pause / resume / set_label -- correct params sent.
# ------------------------------------------------------------------------------------


async def test_pause_sends_the_documented_queue_action(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server)
    await client.pause("some-id")
    call = fake_sabnzbd_server.state.action_calls[-1]
    assert call == {"mode": "queue", "name": "pause", "value": "some-id", "value2": None}


async def test_resume_sends_the_documented_queue_action(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server)
    await client.resume("some-id")
    call = fake_sabnzbd_server.state.action_calls[-1]
    assert call == {"mode": "queue", "name": "resume", "value": "some-id", "value2": None}


async def test_set_label_sends_change_cat_with_the_new_category(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server)
    await client.set_label("some-id", "movies")
    call = fake_sabnzbd_server.state.action_calls[-1]
    assert call == {"mode": "queue", "name": "change_cat", "value": "some-id", "value2": "movies"}


# ------------------------------------------------------------------------------------
# list_base_paths / list_files -- doc-derived, best-effort, never authoritative.
# ------------------------------------------------------------------------------------


async def test_list_base_paths_reads_complete_and_incomplete_dirs(fake_sabnzbd_server):
    fake_sabnzbd_server.state.misc_complete_dir = "/x/complete"
    fake_sabnzbd_server.state.misc_download_dir = "/x/incomplete"
    client = _client(fake_sabnzbd_server)

    paths = await client.list_base_paths()

    by_label = {p.label: p.path for p in paths}
    assert by_label == {"complete": "/x/complete", "incomplete": "/x/incomplete"}


async def test_list_files_returns_filenames_for_a_known_nzo_id(fake_sabnzbd_server):
    fake_sabnzbd_server.state.files_by_nzo_id["some-id"] = ["a.mkv", "b.nfo"]
    client = _client(fake_sabnzbd_server)

    files = await client.list_files("some-id")

    assert files == ["a.mkv", "b.nfo"]


async def test_list_files_unknown_id_returns_empty_list(fake_sabnzbd_server):
    client = _client(fake_sabnzbd_server)
    assert await client.list_files("no-such-id") == []


# ------------------------------------------------------------------------------------
# Declared-`NONE` operations -- raise `CapabilityUnavailable` immediately, no network call.
# ------------------------------------------------------------------------------------


async def test_list_trackers_raises_capability_unavailable_without_touching_the_network():
    # base_url deliberately unreachable -- if this ever tried a real request, it would raise
    # `ClientUnreachable` instead, which is exactly what this test is there to rule out.
    client = SabnzbdClient(config={"base_url": "http://127.0.0.1:1", "api_key": "x"})
    with pytest.raises(CapabilityUnavailable):
        await client.list_trackers("some-id")


async def test_recheck_raises_capability_unavailable_without_touching_the_network():
    client = SabnzbdClient(config={"base_url": "http://127.0.0.1:1", "api_key": "x"})
    with pytest.raises(CapabilityUnavailable):
        await client.recheck("some-id")


# ------------------------------------------------------------------------------------
# The capability declaration matches what this connector can actually populate (prompt item 4)
# -- driven through `project_transfer`, over a record this connector's own methods produced.
# ------------------------------------------------------------------------------------


async def test_capability_declaration_matches_real_populated_fields(fake_sabnzbd_server):
    fake_sabnzbd_server.state.history_slots = [_history_slot()]
    client = _client(fake_sabnzbd_server)

    (result,) = await client.list_history()
    reprojected = project_transfer(result, client.capabilities)

    # `project_transfer` is idempotent against a connector's own already-projected output --
    # nothing declared `NONE` should ever have been populated in the first place.
    assert reprojected == result
    # `added_at` is the one field this connector declares unsupported (doc-derived, UNVERIFIED)
    # -- confirming the real method output actually has it `None`, not merely that the static
    # declaration says so.
    assert result.added_at is None
    assert client.capabilities.supports(Operation.LIST_HISTORY) is True
