"""core/arrclient.py against a real, listening fake *arr (tests/fake_arr.py) -- exercises the
actual HTTP request/response cycle (headers, query-string encoding, JSON parsing, pagination),
not a mocked transport, same philosophy as the fake seedbox's real sshd containers.
"""

from __future__ import annotations

from fake_arr import run_fake_arr_server

from lftpweb.core.arrclient import ArrClient, ArrClientError, HistoryEvent


async def test_system_status_round_trip(fake_arr_server):
    fake_arr_server.state.version = "3.0.10.1567"
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        status = await client.system_status()
    assert status["version"] == "3.0.10.1567"


async def test_wrong_api_key_raises_arr_client_error(fake_arr_server):
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key="wrong-key"
    ) as client:
        try:
            await client.system_status()
        except ArrClientError:
            pass
        else:
            raise AssertionError("expected ArrClientError for a bad API key")


async def test_queue_records_walks_every_page(fake_arr_server):
    """A busy instance can exceed one page (spec's own "Failure modes" warning) -- forced here
    via `page_size_override` so 5 records split across 3 pages of 2, and the client must return
    all 5 without the caller doing anything page-aware.
    """
    fake_arr_server.state.page_size_override = 2
    fake_arr_server.state.queue_records = [
        {
            "downloadId": f"dl{i}",
            "title": f"Release {i}",
            "outputPath": f"/data/torrents/complete/Release.{i}",
            "trackedDownloadState": "downloading",
        }
        for i in range(5)
    ]
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        records = await client.queue_records()
    assert [r.download_id for r in records] == [f"dl{i}" for i in range(5)]
    assert records[0].output_path == "/data/torrents/complete/Release.0"
    assert records[0].tracked_download_state == "downloading"


async def test_queue_records_empty_when_no_records(fake_arr_server):
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        assert await client.queue_records() == []


async def test_import_events_walks_every_page_and_filters_by_download_id(fake_arr_server):
    """`eventType` served as the camelCase **string** a real Sonarr v3 response body actually
    uses (verified against a live instance, 2026-08-15) -- not the numeric code the fixture
    wrongly modeled before this fix, which is why real imports were misclassified `gone`.
    """
    fake_arr_server.state.page_size_override = 2
    fake_arr_server.state.history_events = [
        {"eventType": "downloadFolderImported", "downloadId": "dl0", "sourceTitle": "Release 0"},
        {"eventType": "grabbed", "downloadId": "dl0", "sourceTitle": "Release 0"},
        {"eventType": "downloadFolderImported", "downloadId": "dl0", "sourceTitle": "Release 0"},
        {"eventType": "downloadFolderImported", "downloadId": "other", "sourceTitle": "Unrelated"},
    ]
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        events = await client.import_events(download_id="dl0")
    assert len(events) == 3
    assert all(e.download_id == "dl0" for e in events)
    assert sum(1 for e in events if e.is_import_event()) == 2


async def test_import_events_returns_empty_without_a_filter(fake_arr_server):
    fake_arr_server.state.history_events = [
        {"eventType": "downloadFolderImported", "downloadId": "dl0"}
    ]
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        assert await client.import_events() == []


def test_history_event_is_import_event_accepts_the_string_wire_format():
    """The wire format this codebase now trusts, per the module docstring's live-verification
    note (2026-08-15): a real *arr response body serializes `eventType` as a camelCase string.
    """
    event = HistoryEvent(
        event_type="downloadFolderImported", download_id="dl0", source_title=None, raw={}
    )
    assert event.is_import_event()

    unrelated = HistoryEvent(event_type="grabbed", download_id="dl0", source_title=None, raw={})
    assert not unrelated.is_import_event()


def test_history_event_is_import_event_tolerates_legacy_numeric_event_type():
    """The fallback this fix keeps deliberately, for any *arr version or serializer setting
    that still emits the pre-fix numeric code (`3`) instead of the string -- cheap tolerance,
    never the primary comparison path.
    """
    event = HistoryEvent(event_type=3, download_id="dl0", source_title=None, raw={})
    assert event.is_import_event()

    unrelated = HistoryEvent(event_type=1, download_id="dl0", source_title=None, raw={})
    assert not unrelated.is_import_event()


async def test_post_scan_command_uses_kind_specific_name(fake_arr_server):
    async with ArrClient(
        kind="sonarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        await client.post_scan_command("/data/torrents/complete/Show.S01E05")
    async with ArrClient(
        kind="radarr", base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    ) as client:
        await client.post_scan_command("/data/movies/Movie.2024")

    calls = fake_arr_server.state.command_calls
    assert calls[0]["name"] == "DownloadedEpisodesScan"
    assert calls[0]["path"] == "/data/torrents/complete/Show.S01E05"
    assert calls[0]["importMode"] == "Copy"
    assert calls[1]["name"] == "DownloadedMoviesScan"


async def test_two_independent_fake_instances_do_not_share_state():
    """Sanity check for `run_fake_arr_server` itself -- two servers, two ports, two states,
    used directly (not via the `fake_arr_server` fixture) the same way the poller's
    failure-isolation test needs two independent instances.
    """
    async with run_fake_arr_server() as a, run_fake_arr_server() as b:
        assert a.base_url != b.base_url
        a.state.version = "1.0.0.0"
        b.state.version = "2.0.0.0"
        async with ArrClient(kind="sonarr", base_url=a.base_url, api_key=a.state.api_key) as ca:
            status_a = await ca.system_status()
        async with ArrClient(kind="sonarr", base_url=b.base_url, api_key=b.state.api_key) as cb:
            status_b = await cb.system_status()
        assert status_a["version"] == "1.0.0.0"
        assert status_b["version"] == "2.0.0.0"
